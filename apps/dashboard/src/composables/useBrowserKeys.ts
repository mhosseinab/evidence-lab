import { reactive, ref } from "vue";
import type { KeyGroup, LiveSettings, ProviderProfile, RuntimeMode } from "../types/api";

const groups: KeyGroup[] = ["llm", "cloudflare"];
export function useBrowserKeys() {
  const saved = reactive<Record<KeyGroup, boolean>>({ llm: false, cloudflare: false });
  const profiles = ref<ProviderProfile[]>([]);
  const mode = ref<RuntimeMode>();
  const settings = ref<LiveSettings>();
  let fingerprint = "";
  let keys: Partial<Record<KeyGroup, string>> = {};
  const storageKey = () => `evidence-lab:provider-keys:${fingerprint}`;
  const preferencesKey = () => `evidence-lab:connection:${fingerprint}`;
  function sync() {
    for (const group of groups) saved[group] = !!keys[group];
  }
  function configure(nextFingerprint: string, nextProfiles: ProviderProfile[]) {
    profiles.value = nextProfiles;
    if (fingerprint === nextFingerprint) return;
    fingerprint = nextFingerprint;
    keys = {};
    mode.value = undefined;
    settings.value = undefined;
    if (fingerprint) {
      try {
        const stored: unknown = JSON.parse(localStorage.getItem(storageKey()) || "{}");
        if (stored && typeof stored === "object" && !Array.isArray(stored)) {
          for (const group of groups) {
            const value = (stored as Record<string, unknown>)[group];
            if (typeof value === "string" && validKey(value)) keys[group] = value;
          }
        }
        const preferences = JSON.parse(localStorage.getItem(preferencesKey()) || "{}");
        if (preferences.mode === "mock" || preferences.mode === "live") mode.value = preferences.mode;
        if (preferences.settings) settings.value = validateSettings(preferences.settings);
      } catch {
        /* Missing or unavailable storage leaves credentials unset. */
      }
    }
    sync();
  }
  function validKey(value: string) {
    return value.length > 0 && value.length <= 4096 && /^[!-~]+$/.test(value);
  }
  function writeKeys(next: Partial<Record<KeyGroup, string>>) {
    try {
      localStorage.setItem(storageKey(), JSON.stringify(next));
    } catch {
      throw new Error("Browser storage is unavailable. Allow local storage to manage your keys.");
    }
    keys = next;
    sync();
  }
  function save(group: KeyGroup, value: string) {
    if (!fingerprint) throw new Error("Connect to the workspace before saving keys.");
    if (!validKey(value)) throw new Error("Enter a key containing printable characters without spaces.");
    writeKeys({ ...keys, [group]: value });
  }
  function remove(group: KeyGroup) {
    const next = { ...keys };
    delete next[group];
    writeKeys(next);
  }
  function validateSettings(value: LiveSettings): LiveSettings {
    const workspaceEmbeddings = value.embedding_source === "workspace";
    if (value.embedding_source != null && !["custom", "workspace"].includes(value.embedding_source))
      throw new Error("Choose workspace embeddings or a custom embedding endpoint.");
    for (const field of workspaceEmbeddings
      ? (["chat_endpoint"] as const)
      : (["embedding_endpoint", "chat_endpoint"] as const)) {
      let url: URL;
      try {
        url = new URL(value[field] || "");
      } catch {
        throw new Error("Enter complete embedding and chat endpoint URLs.");
      }
      if (
        !["https:", "http:"].includes(url.protocol) ||
        url.username ||
        url.password ||
        url.search ||
        url.hash
      )
        throw new Error("Use HTTP or HTTPS endpoint URLs without credentials, query strings, or fragments.");
    }
    if ((!workspaceEmbeddings && !value.embedding_model?.trim()) || !value.chat_model?.trim())
      throw new Error("Enter your embedding and chat model identifiers.");
    if (!/^[a-fA-F0-9]{32}$/.test(value.cloudflare_account_id))
      throw new Error("Enter a Cloudflare account ID containing 32 hexadecimal characters.");
    for (const field of [
      "embedding_dimensions",
      "embedding_max_input_tokens",
      "chat_max_input_tokens",
      "chat_max_output_tokens",
    ] as const)
      if (
        (!workspaceEmbeddings || !field.startsWith("embedding_")) &&
        (!Number.isInteger(value[field]) || Number(value[field]) <= 0)
      )
        throw new Error("Dimensions and token limits must be positive whole numbers.");
    for (const field of [
      "budget_usd",
      "embedding_input_usd_per_million",
      "chat_input_usd_per_million",
      "chat_output_usd_per_million",
    ] as const)
      if (
        (!workspaceEmbeddings || !field.startsWith("embedding_")) &&
        (typeof value[field] !== "number" || !Number.isFinite(value[field]) || Number(value[field]) < 0)
      )
        throw new Error(
          "Enter a budget and all three prices as nonnegative numbers. Use 0 for a free endpoint.",
        );
    if (
      !["json_schema", "json_object", "text_json"].includes(value.structured_output) ||
      !["max_tokens", "max_completion_tokens"].includes(value.output_limit_parameter)
    )
      throw new Error("Select supported chat output settings.");
    return {
      embedding_source: value.embedding_source || "custom",
      ...(!workspaceEmbeddings
        ? {
            embedding_endpoint: value.embedding_endpoint,
            embedding_model: value.embedding_model?.trim(),
            embedding_dimensions: value.embedding_dimensions,
            embedding_max_input_tokens: value.embedding_max_input_tokens,
            embedding_input_usd_per_million: value.embedding_input_usd_per_million,
          }
        : {}),
      chat_endpoint: value.chat_endpoint,
      chat_model: value.chat_model.trim(),
      chat_max_input_tokens: value.chat_max_input_tokens,
      chat_max_output_tokens: value.chat_max_output_tokens,
      structured_output: value.structured_output,
      output_limit_parameter: value.output_limit_parameter,
      cloudflare_account_id: value.cloudflare_account_id,
      budget_usd: value.budget_usd,
      chat_input_usd_per_million: value.chat_input_usd_per_million,
      chat_output_usd_per_million: value.chat_output_usd_per_million,
    };
  }
  function savePreferences(nextMode: RuntimeMode | undefined, nextSettings = settings.value) {
    const validated = nextSettings ? validateSettings(nextSettings) : undefined;
    try {
      localStorage.setItem(preferencesKey(), JSON.stringify({ mode: nextMode, settings: validated }));
    } catch {
      throw new Error("Browser storage is unavailable. Connection preferences could not be saved.");
    }
    settings.value = validated;
    mode.value = nextMode;
  }
  function removeSetup() {
    try {
      localStorage.removeItem(preferencesKey());
    } catch {
      throw new Error("Browser storage is unavailable. Live setup could not be removed.");
    }
    settings.value = undefined;
    mode.value = undefined;
  }
  function settingsHeader() {
    if (!settings.value)
      throw new Error("Save your live endpoint setup in workspace connection before selecting live mode.");
    return JSON.stringify(settings.value);
  }
  function header() {
    const required = new Set(profiles.value.map((profile) => profile.key_group || "llm"));
    if (!required.size || [...required].some((group) => !keys[group]))
      throw new Error("Save the required LLM provider key and Cloudflare key in workspace connection.");
    return JSON.stringify(Object.fromEntries([...required].map((group) => [group, keys[group]])));
  }
  function dispose() {
    keys = {};
  }
  return {
    saved,
    profiles,
    mode,
    settings,
    configure,
    save,
    remove,
    savePreferences,
    settingsHeader,
    removeSetup,
    header,
    dispose,
  };
}
