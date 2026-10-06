import { afterEach, describe, expect, it } from "vitest";
import { useBrowserKeys } from "../src/composables/useBrowserKeys";
import { liveSettings } from "./fixtures/liveSettings";

afterEach(() => localStorage.clear());
const profiles = [{ name: "chat", model: "chat-v1", roles: ["generator", "verifier"] }];

describe("browser provider keys", () => {
  it("persists locally, restores on reload, and can be removed", () => {
    const keys = useBrowserKeys();
    keys.configure("fingerprint", profiles);
    keys.save("llm", "browser-only-secret");
    expect(keys.saved.llm).toBe(true);
    const reloaded = useBrowserKeys();
    reloaded.configure("fingerprint", profiles);
    expect(JSON.parse(reloaded.header())).toEqual({ llm: "browser-only-secret" });
    reloaded.remove("llm");
    expect(() => reloaded.header()).toThrow("Save");
    expect(localStorage.getItem("evidence-lab:provider-keys:fingerprint")).not.toContain(
      "browser-only-secret",
    );
  });
  it("does not reuse keys for another configuration", () => {
    const keys = useBrowserKeys();
    keys.configure("first", profiles);
    keys.save("llm", "browser-only-secret");
    keys.configure("second", profiles);
    expect(keys.saved.llm).toBe(false);
    expect(() => keys.header()).toThrow("Save");
  });
  it("rejects invalid keys without putting secrets in errors", () => {
    const keys = useBrowserKeys();
    keys.configure("first", profiles);
    expect(() => keys.save("llm", "secret\ninvalid")).toThrow("printable");
    expect(keys.saved.llm).toBe(false);
  });
});

it("requires only active key groups, and refreshes profiles without losing browser keys", () => {
  const keys = useBrowserKeys();
  keys.configure("base-scope", profiles);
  keys.save("llm", "llm-secret");
  keys.save("cloudflare", "cf-secret");
  keys.configure("base-scope", [
    { name: "embed", model: "embed", roles: ["embeddings"], key_group: "llm" },
    { name: "clef", model: "clef", roles: ["verifier"], key_group: "cloudflare" },
  ]);
  expect(JSON.parse(keys.header())).toEqual({ llm: "llm-secret", cloudflare: "cf-secret" });
  keys.remove("cloudflare");
  expect(() => keys.header()).toThrow("Cloudflare");
});
it("restores local setup without credentials in its metadata and rejects invalid pricing", () => {
  const keys = useBrowserKeys();
  keys.configure("base-scope", profiles);
  keys.save("llm", "llm-secret");
  keys.savePreferences("live", liveSettings);
  expect(keys.settingsHeader()).not.toContain("llm-secret");
  const reloaded = useBrowserKeys();
  reloaded.configure("base-scope", profiles);
  expect(reloaded.mode.value).toBe("live");
  expect(reloaded.settings.value).toEqual(liveSettings);
  expect(() => keys.savePreferences("live", { ...liveSettings, chat_input_usd_per_million: -1 })).toThrow(
    "prices",
  );
});
