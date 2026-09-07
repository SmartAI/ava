// Offline preflight: reject fuzzy model matches and silently clamped thinking levels
// before handing API credentials to the agent process.
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { getSupportedThinkingLevels } from "@earendil-works/pi-ai";
const [provider, modelId, effort] = process.argv.slice(2);
const runtime = await ModelRuntime.create({
    modelsPath: null, authPath: `${process.env.PI_CODING_AGENT_DIR}/preflight-auth.json`,
});
const model = runtime.getModel(provider, modelId);
if (!model) throw new Error(`Exact model is absent from Pi's pinned catalog: ${provider}/${modelId}`);
if (!getSupportedThinkingLevels(model).includes(effort)) {
    throw new Error(`Pi model does not support the requested thinking level: ${effort}`);
}
console.log(JSON.stringify({ provider, model: model.id, api: model.api, effort, contextWindow: model.contextWindow }));
