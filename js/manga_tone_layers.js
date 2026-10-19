// Manga Finish Layers:
// - grey out white_point / black_point while level_mode is auto, since the auto mode computes them
//   from the image and ignores the widget values
// - load workflows saved before level_mode moved up next to screen_lines: widget values are stored
//   by position, so the older order is remapped by name
import { app } from "../../scripts/app.js";

const NODE = "MangaFinishLayers";
const MANUAL_ONLY = ["white_point", "black_point"];

// widget order before level_mode moved; it was appended last (or did not exist yet)
const LEGACY_ORDER = [
    "screen_lines", "white_point", "black_point", "light_tone", "dark_tone", "gradation", "tone_split",
    "pen_threshold", "white_lines", "tone_output", "mask_scale", "lines_in_solid", "ink_color",
    "line_cleanup", "tone_flatten", "line_antialias", "level_mode",
];
// what a widget missing from an old save was given on load at the time
const LEGACY_DEFAULTS = { line_cleanup: 0.4, tone_flatten: 0.0, line_antialias: 0.0, level_mode: "manual" };

function legacyLevelMode(value) {
    return String(value).startsWith("auto") ? "auto" : "manual";
}

function remapLegacyValues(node, values) {
    // new order has level_mode (a string) second; the old one had white_point (a number) there
    if (!Array.isArray(values) || typeof values[1] !== "number") return;
    const byName = { ...LEGACY_DEFAULTS };
    LEGACY_ORDER.forEach((name, i) => {
        if (i < values.length) byName[name] = values[i];
    });
    byName.level_mode = legacyLevelMode(byName.level_mode);
    for (const w of node.widgets ?? []) {
        if (w.name in byName) w.value = byName[w.name];
    }
}

function syncLevelMode(node) {
    const mode = node.widgets?.find((w) => w.name === "level_mode");
    if (!mode) return;
    if (!["manual", "auto"].includes(mode.value)) mode.value = legacyLevelMode(mode.value);
    const auto = mode.value === "auto";
    for (const w of node.widgets) {
        if (MANUAL_ONLY.includes(w.name)) w.disabled = auto;
    }
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: "MangaToneLayers.LevelMode",
    nodeCreated(node) {
        if (node.comfyClass !== NODE) return;
        const mode = node.widgets?.find((w) => w.name === "level_mode");
        if (mode) {
            const callback = mode.callback;
            mode.callback = function (...args) {
                const r = callback?.apply(this, args);
                syncLevelMode(node);
                return r;
            };
        }
        // a loaded workflow sets the widget values after the node is created
        const onConfigure = node.onConfigure;
        node.onConfigure = function (info, ...rest) {
            const r = onConfigure?.call(this, info, ...rest);
            remapLegacyValues(node, info?.widgets_values);
            syncLevelMode(node);
            return r;
        };
        syncLevelMode(node);
    },
});
