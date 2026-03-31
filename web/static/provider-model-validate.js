/**
 * Client-side checks — keep rules aligned with provider_model_validate.py
 */
function validateProviderModel(provider, model, fieldLabel) {
  var label = fieldLabel ? fieldLabel + ": " : "";
  var mRaw = String(model || "").trim();
  if (!mRaw) {
    return { ok: false, message: label + "Model is required" };
  }
  var low = mRaw.toLowerCase();
  var p = String(provider || "")
    .trim()
    .toLowerCase();
  if (p === "anthropic") {
    if (low.indexOf("claude") !== 0) {
      return {
        ok: false,
        message:
          label +
          'Anthropic expects a Claude model id starting with "claude" (e.g. claude-sonnet-4-20250514); got "' +
          mRaw +
          '"',
      };
    }
  } else if (p === "deepseek") {
    if (low.indexOf("deepseek") !== 0) {
      return {
        ok: false,
        message:
          label +
          'DeepSeek expects a model id starting with "deepseek" (e.g. deepseek-chat); got "' +
          mRaw +
          '"',
      };
    }
  } else if (p === "openrouter") {
    if (low.indexOf("openrouter") !== 0 && mRaw.indexOf("/") === -1) {
      return {
        ok: false,
        message:
          label +
          'OpenRouter expects a vendor/model slug containing "/" (e.g. anthropic/claude-3.5-sonnet) or a model id starting with "openrouter"; got "' +
          mRaw +
          '"',
      };
    }
  } else {
    return { ok: false, message: label + "Unknown provider: " + provider };
  }
  return { ok: true };
}
