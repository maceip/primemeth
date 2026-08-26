export const PRIME_METH_PROMPT = "prime METH> ";

const METH_GRADIENT = [
	{ r: 185, g: 230, b: 255 },
	{ r: 98, g: 184, b: 247 },
	{ r: 49, g: 116, b: 210 },
	{ r: 25, g: 54, b: 132 },
] as const;

function truecolorLetter(letter: string, color: (typeof METH_GRADIENT)[number]): string {
	return `\x1b[38;2;${color.r};${color.g};${color.b}m${letter}\x1b[39m`;
}

export function formatPrimeMethPrompt(colorMode: "truecolor" | "256color"): string {
	const prime = "\x1b[1;97mprime\x1b[22;39m";
	const meth =
		colorMode === "truecolor"
			? [..."METH"].map((letter, index) => truecolorLetter(letter, METH_GRADIENT[index]!)).join("")
			: "\x1b[34mMETH\x1b[39m";
	return `${prime} ${meth}\x1b[97m>\x1b[39m `;
}
