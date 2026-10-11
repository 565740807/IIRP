import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./load.mjs";

const { CHART, MIN_CONTRAST, PRESETS, SURFACES, contrast, labelOn, mix, paletteOf, resolve, weakestContrast } = await load("lib/colors.ts");

test("contrast follows WCAG: black on white is 21:1", () => {
  assert.equal(Math.round(contrast("#000000", "#ffffff")), 21);
  assert.equal(contrast("#777777", "#777777"), 1);
});

test("every preset's text colors reach 4.5:1 on cards, the page and their own tints and flashes", () => {
  for (const [preset, colors] of Object.entries(PRESETS)) {
    const palette = paletteOf({ preset });
    for (const side of ["up", "down"]) {
      const color = colors[side];
      for (const background of [SURFACES.card, SURFACES.page, palette[`${side}Soft`], palette[`${side}Flash`]]) {
        assert.ok(contrast(color, background) >= MIN_CONTRAST, `${preset} ${side} ${color} on ${background}: ${contrast(color, background).toFixed(2)}`);
      }
    }
  }
});

test("presets: US green up / red down, color-blind blue / orange, Asia swaps US", () => {
  assert.deepEqual(resolve({ preset: "asia" }), { up: PRESETS.us.down, down: PRESETS.us.up });
  assert.notDeepEqual(PRESETS.colorblind, PRESETS.us);
  assert.deepEqual(resolve({ preset: "nonsense" }), PRESETS.us);
});

test("custom colors are used only when both are valid", () => {
  assert.deepEqual(resolve({ preset: "custom", up: "#00AA00", down: "#aa0000" }), { up: "#00aa00", down: "#aa0000" });
  assert.deepEqual(resolve({ preset: "custom", up: "#00aa00", down: null }), PRESETS.us);
  assert.deepEqual(resolve({ preset: "custom", up: "green", down: "#aa0000" }), PRESETS.us);
});

test("a light custom color is reported below the minimum", () => {
  assert.ok(weakestContrast("#ffd400") < MIN_CONTRAST);
  assert.ok(weakestContrast(PRESETS.us.up) >= MIN_CONTRAST);
});

test("tints mix toward white and heatmap labels pick the readable text color", () => {
  assert.equal(mix("#ffffff", "#137333", 0), "#ffffff");
  assert.equal(mix("#ffffff", "#137333", 1), "#137333");
  assert.equal(labelOn(PRESETS.us.up), CHART.lightLabel);
  assert.equal(labelOn(CHART.tile), CHART.darkLabel);
  assert.equal(labelOn("#ffd400"), CHART.darkLabel);
});
