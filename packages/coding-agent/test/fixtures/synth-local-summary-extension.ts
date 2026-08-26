import { fauxAssistantMessage, getApiProvider, registerFauxProvider } from "../../../ai/src/index.js";
import type { ExtensionAPI } from "../../src/index.js";

export default function registerSynthLocalSummaryProvider(pi: ExtensionAPI): void {
	const faux = registerFauxProvider({
		provider: "local",
		models: [
			{ id: "local-main", reasoning: false },
			{ id: "local-small", reasoning: false },
		],
	});
	faux.setResponses([
		fauxAssistantMessage("Synth A2 primary response"),
		fauxAssistantMessage("<recap>Completed the Synth local recap demonstration</recap>\n<status>COMPLETED</status>"),
	]);
	const apiProvider = getApiProvider(faux.api);
	if (!apiProvider) {
		throw new Error("Faux API provider was not registered");
	}
	pi.registerProvider("local", {
		api: faux.api,
		apiKey: "faux-key",
		baseUrl: faux.getModel().baseUrl,
		streamSimple: apiProvider.streamSimple,
		models: faux.models.map((model) => ({
			api: model.api,
			baseUrl: model.baseUrl,
			contextWindow: model.contextWindow,
			cost: model.cost,
			id: model.id,
			input: model.input,
			maxTokens: model.maxTokens,
			name: model.name,
			reasoning: model.reasoning,
		})),
	});
}
