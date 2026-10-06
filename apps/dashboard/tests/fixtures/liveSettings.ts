import type { LiveSettings } from "../../src/types/api";
export const liveSettings: LiveSettings = {
  embedding_source: "custom",
  embedding_endpoint: "https://provider.example/v1/embeddings",
  embedding_model: "embedding-model",
  embedding_dimensions: 1536,
  embedding_max_input_tokens: 8192,
  chat_endpoint: "https://provider.example/v1/chat/completions",
  chat_model: "chat-model",
  chat_max_input_tokens: 65536,
  chat_max_output_tokens: 1200,
  structured_output: "text_json",
  output_limit_parameter: "max_tokens",
  cloudflare_account_id: "a".repeat(32),
  budget_usd: 0,
  embedding_input_usd_per_million: 0,
  chat_input_usd_per_million: 0,
  chat_output_usd_per_million: 0,
};
