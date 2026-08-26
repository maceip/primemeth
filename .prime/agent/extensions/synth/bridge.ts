import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { createInterface, type Interface as ReadlineInterface } from "node:readline";
import { randomUUID } from "node:crypto";

const BRIDGE_TIMEOUT_MS = 5_000;
const MAX_RESPONSE_CHARACTERS = 1_048_576;

export type BridgeState = "idle" | "starting" | "up" | "down" | "disabled";

export interface BridgeStatus {
	state: BridgeState;
	pid?: number;
	error?: string;
}

export interface BridgePing {
	protocolVersion: number;
	bridgeVersion: string;
	pythonVersion: string;
	commands: string[];
	memoryAvailable: boolean;
}

interface PendingRequest {
	resolve: (result: unknown) => void;
	reject: (error: Error) => void;
	timer: ReturnType<typeof setTimeout>;
}

interface BridgeResponse {
	id: string | null;
	ok: boolean;
	result?: unknown;
	error?: { code: string; message: string; retryable: boolean };
}

export class BridgeRequestError extends Error {
	constructor(
		readonly code: string,
		message: string,
		readonly retryable: boolean,
	) {
		super(message);
		this.name = "BridgeRequestError";
	}
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseResponse(line: string): BridgeResponse | undefined {
	let value: unknown;
	try {
		value = JSON.parse(line);
	} catch {
		return undefined;
	}
	if (!isRecord(value) || (typeof value.id !== "string" && value.id !== null) || typeof value.ok !== "boolean") {
		return undefined;
	}
	if (value.ok) {
		return { id: value.id, ok: true, result: value.result };
	}
	if (
		!isRecord(value.error) ||
		typeof value.error.code !== "string" ||
		typeof value.error.message !== "string" ||
		typeof value.error.retryable !== "boolean"
	) {
		return undefined;
	}
	return {
		id: value.id,
		ok: false,
		error: {
			code: value.error.code,
			message: value.error.message,
			retryable: value.error.retryable,
		},
	};
}

function parsePing(value: unknown): BridgePing {
	if (
		!isRecord(value) ||
		typeof value.protocolVersion !== "number" ||
		typeof value.bridgeVersion !== "string" ||
		typeof value.pythonVersion !== "string" ||
		!Array.isArray(value.commands) ||
		!value.commands.every((command) => typeof command === "string") ||
		typeof value.memoryAvailable !== "boolean"
	) {
		throw new Error("bridge ping returned an invalid payload");
	}
	return {
		protocolVersion: value.protocolVersion,
		bridgeVersion: value.bridgeVersion,
		pythonVersion: value.pythonVersion,
		commands: value.commands,
		memoryAvailable: value.memoryAvailable,
	};
}

export class BridgeClient {
	private child: ChildProcessWithoutNullStreams | undefined;
	private reader: ReadlineInterface | undefined;
	private readonly pending = new Map<string, PendingRequest>();
	private state: BridgeState = "idle";
	private lastError: string | undefined;
	private warned = false;
	private closing = false;

	constructor(
		private readonly projectRoot: string,
		private readonly onWarning: (message: string) => void,
	) {}

	getStatus(): BridgeStatus {
		return {
			state: this.state,
			...(this.child?.pid ? { pid: this.child.pid } : {}),
			...(this.lastError ? { error: this.lastError } : {}),
		};
	}

	private warn(message: string): void {
		if (this.warned) {
			return;
		}
		this.warned = true;
		this.onWarning(message);
	}

	private start(): void {
		if (this.state === "disabled") {
			throw new Error(this.lastError ?? "Synth bridge is disabled for this session");
		}
		if (this.child) {
			return;
		}
		this.state = "starting";
		this.closing = false;
		const child = spawn("uv", ["run", "--project", this.projectRoot, "--no-sync", "synth-bridge"], {
			cwd: this.projectRoot,
			env: { ...process.env, UV_NO_PROGRESS: "1" },
			stdio: ["pipe", "pipe", "pipe"],
		});
		this.child = child;
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		this.reader = createInterface({ input: child.stdout });
		this.reader.on("line", (line) => this.handleLine(line));
		child.once("spawn", () => {
			if (this.state !== "disabled") {
				this.state = "up";
			}
		});
		child.once("error", (error) => this.disable(`bridge process error: ${error.message}`));
		child.once("exit", (code, signal) => {
			this.child = undefined;
			this.reader?.close();
			this.reader = undefined;
			if (this.closing) {
				this.state = "down";
				return;
			}
			this.disable(`bridge exited unexpectedly (${signal ?? code ?? "unknown"})`);
		});
		child.stderr.on("data", (chunk: string | Buffer) => {
			const detail = String(chunk).trim();
			if (detail) {
				this.lastError = detail.slice(-1_000);
			}
		});
	}

	private handleLine(line: string): void {
		if (line.length > MAX_RESPONSE_CHARACTERS) {
			this.disable("bridge response exceeded the framing limit");
			return;
		}
		const response = parseResponse(line);
		if (!response || typeof response.id !== "string") {
			this.disable("bridge emitted an invalid JSON-lines response");
			return;
		}
		const pending = this.pending.get(response.id);
		if (!pending) {
			return;
		}
		this.pending.delete(response.id);
		clearTimeout(pending.timer);
		if (response.ok) {
			pending.resolve(response.result);
			return;
		}
		const error = response.error;
		if (!error) {
			pending.reject(new Error("bridge returned an unspecified error"));
			return;
		}
		pending.reject(new BridgeRequestError(error.code, error.message, error.retryable));
	}

	private disable(message: string): void {
		if (this.state === "disabled") {
			return;
		}
		this.state = "disabled";
		this.lastError = message;
		for (const pending of this.pending.values()) {
			clearTimeout(pending.timer);
			pending.reject(new Error(message));
		}
		this.pending.clear();
		this.reader?.close();
		this.reader = undefined;
		const child = this.child;
		this.child = undefined;
		if (child && !child.killed) {
			child.kill("SIGKILL");
		}
		this.warn(message);
	}

	request(command: string, params: Record<string, unknown>): Promise<unknown> {
		try {
			this.start();
		} catch (error) {
			return Promise.reject(error);
		}
		const child = this.child;
		if (!child) {
			return Promise.reject(new Error("Synth bridge did not start"));
		}
		const id = randomUUID();
		return new Promise<unknown>((resolve, reject) => {
			const timer = setTimeout(() => {
				this.disable(`bridge request timed out after ${BRIDGE_TIMEOUT_MS}ms`);
			}, BRIDGE_TIMEOUT_MS);
			timer.unref?.();
			this.pending.set(id, { resolve, reject, timer });
			let frame: string;
			try {
				frame = `${JSON.stringify({ id, command, params })}\n`;
			} catch (error) {
				this.pending.delete(id);
				clearTimeout(timer);
				reject(error);
				return;
			}
			child.stdin.write(frame, "utf8", (error) => {
				if (error) {
					this.disable(`bridge stdin failed: ${error.message}`);
				}
			});
		});
	}

	async ping(): Promise<BridgePing> {
		return parsePing(await this.request("ping", { protocolVersion: 1 }));
	}

	async close(): Promise<void> {
		this.closing = true;
		for (const pending of this.pending.values()) {
			clearTimeout(pending.timer);
			pending.reject(new Error("Synth bridge is shutting down"));
		}
		this.pending.clear();
		const child = this.child;
		if (!child) {
			this.state = "down";
			return;
		}
		await new Promise<void>((resolve) => {
			let settled = false;
			const finish = () => {
				if (settled) {
					return;
				}
				settled = true;
				resolve();
			};
			child.once("exit", finish);
			child.stdin.end();
			const timer = setTimeout(() => {
				if (!child.killed) {
					child.kill("SIGKILL");
				}
				finish();
			}, 250);
			timer.unref?.();
		});
		this.child = undefined;
		this.state = "down";
	}
}
