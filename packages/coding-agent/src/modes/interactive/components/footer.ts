import { homedir } from "node:os";
import { type Component, truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import type { ReadonlyFooterDataProvider } from "../../../core/footer-data-provider.js";
import { theme } from "../theme/theme.js";

function sanitizeStatusText(text: string): string {
	return text.replace(/[\r\n\t]+/g, " ").trim();
}

function formatCwd(cwd: string): string {
	const home = homedir();
	return home && cwd.startsWith(`${home}/`) ? `~${cwd.slice(home.length)}` : cwd;
}

export class FooterComponent implements Component {
	constructor(
		private readonly footerData: ReadonlyFooterDataProvider,
		private readonly getModelId: () => string | undefined = () => undefined,
		private readonly getCwd: () => string = () => process.cwd(),
	) {}

	setAutoCompactEnabled(_enabled: boolean): void {
		// The compact rail does not render compaction state.
	}

	/**
	 * No-op: git branch caching now handled by provider.
	 * Kept for compatibility with existing call sites in interactive-mode.
	 */
	invalidate(): void {
		// Live model and cwd values are read on every render.
	}

	/**
	 * Clean up resources.
	 * Git watcher cleanup now handled by provider.
	 */
	dispose(): void {
		// Git watcher cleanup is handled by the provider.
	}

	render(width: number): string[] {
		const safeWidth = Math.max(1, width);
		const separator = theme.fg("dim", "  ·  ");
		const model = sanitizeStatusText(this.getModelId() ?? "—");
		const cwd = formatCwd(sanitizeStatusText(this.getCwd()));
		const modelPart = `${theme.fg("dim", "model")} ${theme.fg("text", model)}`;
		const cwdLabel = theme.fg("dim", "cwd");
		const statuses = [...this.footerData.getExtensionStatuses().entries()]
			.sort(([left], [right]) => left.localeCompare(right))
			.map(([, text]) => sanitizeStatusText(text))
			.filter((text) => text.length > 0);
		const statusPart = statuses.join(separator);
		const fullCwdPart = `${cwdLabel} ${theme.fg("text", cwd)}`;
		const fullLine = [modelPart, fullCwdPart, statusPart].filter(Boolean).join(separator);
		if (visibleWidth(fullLine) <= safeWidth || !statusPart) {
			return [truncateToWidth(fullLine, safeWidth, theme.fg("dim", "…"))];
		}

		const fixedLine = `${modelPart}${separator}${cwdLabel} ${separator}${statusPart}`;
		const cwdWidth = Math.max(1, safeWidth - visibleWidth(fixedLine));
		const compactCwd = truncateToWidth(cwd, cwdWidth, "…");
		const line = `${modelPart}${separator}${cwdLabel} ${theme.fg("text", compactCwd)}${separator}${statusPart}`;
		return [truncateToWidth(line, safeWidth, theme.fg("dim", "…"))];
	}
}
