/* Core Chart.js defaults and helpers (`window.CommissureCharts`).
 * Loaded automatically after `chart.umd.min.js` when `render_page` includes it.
 * Reads `--chart-*` and `--brand-*` tokens from `:root` so recipe/brand/brand.css
 * overrides apply without recipe-local Chart.js boilerplate.
 */
(function () {
  "use strict";

  var FALLBACK_CAT = [
    "#005ea2",
    "#5a9bdc",
    "#b9530f",
    "#e8894a",
    "#842a64",
    "#c6669f",
    "#1dc6a6",
    "#6e65c2"
  ];

  function rootStyles() {
    if (typeof document === "undefined" || !document.documentElement) {
      return null;
    }
    return getComputedStyle(document.documentElement);
  }

  function token(name, fallback) {
    var styles = rootStyles();
    if (!styles) {
      return fallback || "";
    }
    var val = styles.getPropertyValue(name);
    if (!val) {
      return fallback || "";
    }
    val = val.trim();
    if (val.indexOf("var(") === 0) {
      var inner = val.slice(4, -1).split(",")[0].trim();
      var resolved = styles.getPropertyValue(inner).trim();
      if (resolved) {
        return resolved;
      }
    }
    return val || fallback || "";
  }

  function pageFontFamily() {
    if (typeof document !== "undefined" && document.body) {
      var bodyFont = getComputedStyle(document.body).fontFamily;
      if (bodyFont) {
        return bodyFont;
      }
    }
    var styles = rootStyles();
    if (styles) {
      var rootFont =
        styles.getPropertyValue("--pico-font-family").trim() || styles.fontFamily;
      if (rootFont) {
        return rootFont;
      }
    }
    return "system-ui, -apple-system, sans-serif";
  }

  function palette() {
    var out = [];
    for (var i = 0; i < FALLBACK_CAT.length; i++) {
      out.push(token("--chart-cat-" + (i + 1), FALLBACK_CAT[i]));
    }
    return out;
  }

  function color(index) {
    var pal = palette();
    var n = pal.length;
    var idx = ((index % n) + n) % n;
    return pal[idx];
  }

  function hashKey(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function colorFor(key, keys) {
    var k = String(key == null ? "" : key);
    if (Array.isArray(keys)) {
      var pos = keys.indexOf(key);
      if (pos === -1) {
        pos = keys.indexOf(k);
      }
      if (pos !== -1) {
        return color(pos);
      }
    }
    return color(hashKey(k));
  }

  // Prints each column's value above its cap. Enable per chart by passing
  // `plugins: [CommissureCharts.capLabels]` or `options.plugins.capLabels = true`.
  var capLabels = {
    id: "capLabels",
    afterDatasetsDraw: function (chart, args, opts) {
      if (opts === false) {
        return;
      }
      var hasPlugin =
        (chart.config &&
          Array.isArray(chart.config.plugins) &&
          chart.config.plugins.indexOf(capLabels) !== -1) ||
        Boolean(
          chart.options &&
            chart.options.plugins &&
            chart.options.plugins.capLabels
        );
      if (!hasPlugin) {
        return;
      }
      var datasets = (chart.data && chart.data.datasets) || [];
      if (!datasets.length) {
        return;
      }
      var ctx = chart.ctx;
      var ink = token("--brand-ink", "#1b1b1b");
      var family = pageFontFamily();
      ctx.save();
      ctx.fillStyle = ink;
      ctx.font = "12px " + family;
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      chart.getDatasetMeta(0).data.forEach(function (bar, i) {
        var val = datasets[0].data[i];
        if (val != null) {
          ctx.fillText(String(val), bar.x, bar.y - 4);
        }
      });
      ctx.restore();
    }
  };

  // Shaded x-axis bands (`bands: [{ start, end, label }]`) and vertical reference
  // lines (`lines: [{ x, label }]`).
  var referenceLines = {
    id: "referenceLines",
    beforeDatasetsDraw: function (chart, args, opts) {
      if (!opts || opts === false) {
        return;
      }
      var bands = opts.bands || [];
      var x = chart.scales && chart.scales.x;
      var area = chart.chartArea;
      if (!bands.length || !x || !area) {
        return;
      }
      var labels = (chart.data && chart.data.labels) || [];
      var half =
        opts.slotHalf === false
          ? 0
          : (area.right - area.left) / Math.max(labels.length, 1) / 2;
      var bandFill = opts.bandColor || token("--chart-band", "rgb(86 92 101 / 0.09)");
      chart.ctx.save();
      chart.ctx.fillStyle = bandFill;
      bands.forEach(function (b) {
        var left = Math.max(area.left, x.getPixelForValue(b.start) - half);
        var right = Math.min(area.right, x.getPixelForValue(b.end) + half);
        if (right > left) {
          chart.ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
        }
      });
      chart.ctx.restore();
    },
    afterDatasetsDraw: function (chart, args, opts) {
      if (!opts || opts === false) {
        return;
      }
      var x = chart.scales && chart.scales.x;
      var area = chart.chartArea;
      if (!x || !area) {
        return;
      }
      var labels = (chart.data && chart.data.labels) || [];
      var half =
        opts.slotHalf === false
          ? 0
          : (area.right - area.left) / Math.max(labels.length, 1) / 2;
      var markers = [];
      (opts.bands || []).forEach(function (b) {
        if (b.end != null && (b.label != null || b.close_label != null || opts.drawBandEnds !== false)) {
          markers.push({
            x: b.end,
            offset: half,
            label: opts.label === false ? "" : b.label || b.close_label || ""
          });
        }
      });
      (opts.lines || []).forEach(function (ln) {
        markers.push({
          x: ln.x,
          offset: ln.slotEnd ? half : 0,
          label: opts.label === false ? "" : ln.label || ""
        });
      });
      if (!markers.length) {
        return;
      }
      var ctx = chart.ctx;
      var ink = opts.lineColor || token("--brand-ink", "#1b1b1b");
      var family = pageFontFamily();
      var drawn = {};
      ctx.save();
      markers.forEach(function (m) {
        var key = String(m.x) + ":" + String(m.offset);
        if (drawn[key]) {
          return;
        }
        drawn[key] = true;
        var px = Math.round(x.getPixelForValue(m.x) + m.offset) + 0.5;
        if (px < area.left - 1 || px > area.right + 1) {
          return;
        }
        ctx.strokeStyle = ink;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(px, area.top);
        ctx.lineTo(px, area.bottom);
        ctx.stroke();
        if (m.label) {
          ctx.fillStyle = ink;
          ctx.font = "12px " + family;
          ctx.textBaseline = "bottom";
          ctx.textAlign = px > area.right - 200 ? "right" : "left";
          ctx.fillText(
            m.label,
            ctx.textAlign === "right" ? px - 4 : px + 4,
            area.top - 4
          );
        }
      });
      ctx.restore();
    }
  };

  // Assigns token-backed colors to datasets that omit explicit colors.
  // When datasets carry `key` (or `datasets.length > 1`), uses `colorFor(key, keys)`
  // or `color(i)` so filtered series keep stable colors.
  var datasetColorsPlugin = {
    id: "commissureDatasetColors",
    beforeUpdate: function (chart) {
      applyDefaults();
      var datasets = (chart.data && chart.data.datasets) || [];
      if (!datasets.length) {
        return;
      }
      var series = token("--chart-series", token("--brand-primary", "#005ea2"));
      var wash = token("--chart-wash", "rgb(0 94 162 / 0.1)");
      var sheet = token("--brand-sheet", "#ffffff");
      var chartType = chart.config && chart.config.type;
      var explicitKeys =
        (chart.options &&
          chart.options.plugins &&
          chart.options.plugins.commissureDatasetColors &&
          chart.options.plugins.commissureDatasetColors.keys) ||
        null;
      var allKeys =
        explicitKeys ||
        datasets
          .map(function (ds) {
            return ds.key;
          })
          .filter(function (k) {
            return k != null;
          });
      var multi = datasets.length > 1 || allKeys.length > 0;

      datasets.forEach(function (ds, i) {
        var dsType = ds.type || chartType;
        var chosen = multi
          ? ds.key != null
            ? colorFor(ds.key, allKeys.length ? allKeys : null)
            : color(i)
          : series;
        if (dsType === "line") {
          if (ds.borderColor == null) {
            ds.borderColor = chosen;
          }
          if (ds.backgroundColor == null) {
            ds.backgroundColor = multi ? chosen : wash;
          }
          if (ds.pointHoverBackgroundColor == null) {
            ds.pointHoverBackgroundColor = ds.borderColor;
          }
          if (ds.pointHoverBorderColor == null) {
            ds.pointHoverBorderColor = sheet;
          }
        } else {
          if (ds.backgroundColor == null) {
            ds.backgroundColor = chosen;
          }
        }
      });
    }
  };

  function applyDefaults() {
    if (typeof Chart === "undefined" || !Chart.defaults) {
      return;
    }
    var d = Chart.defaults;
    var axisInk = token("--chart-axis-ink", token("--brand-ink-soft", "#565c65"));
    var grid = token("--chart-grid", token("--brand-line", "#d4dae2"));
    var sheet = token("--brand-sheet", "#ffffff");

    d.color = axisInk;
    d.borderColor = grid;
    d.font.family = pageFontFamily();
    d.animation = false;

    if (d.interaction) {
      d.interaction.mode = "index";
      d.interaction.intersect = false;
    }
    if (d.plugins && d.plugins.tooltip) {
      d.plugins.tooltip.mode = "index";
      d.plugins.tooltip.intersect = false;
      d.plugins.tooltip.displayColors = function (ctx) {
        var ds = (ctx && ctx.chart && ctx.chart.data && ctx.chart.data.datasets) || [];
        return ds.length > 1;
      };
    }
    if (d.datasets && d.datasets.bar) {
      d.datasets.bar.maxBarThickness = 24;
      d.datasets.bar.borderRadius = 4;
      d.datasets.bar.borderSkipped = "start";
    }
    if (d.elements) {
      if (d.elements.bar) {
        d.elements.bar.borderRadius = 4;
        d.elements.bar.borderSkipped = "start";
      }
      if (d.elements.line) {
        d.elements.line.borderWidth = 2;
        d.elements.line.borderJoinStyle = "round";
        d.elements.line.borderCapStyle = "round";
      }
      if (d.elements.point) {
        d.elements.point.radius = 0;
        d.elements.point.hoverRadius = 4;
        d.elements.point.hoverBorderWidth = 2;
        d.elements.point.hoverBorderColor = sheet;
      }
    }
    if (d.scale) {
      if (d.scale.grid) {
        d.scale.grid.color = grid;
      }
      if (d.scale.border) {
        d.scale.border.display = false;
      }
    }
    if (d.scales && d.scales.category && d.scales.category.grid) {
      d.scales.category.grid.display = false;
    }
  }

  if (typeof Chart !== "undefined") {
    applyDefaults();
    if (typeof Chart.register === "function") {
      Chart.register(datasetColorsPlugin, capLabels, referenceLines);
    }
  }

  if (typeof window !== "undefined") {
    window.CommissureCharts = {
      token: token,
      palette: palette,
      color: color,
      colorFor: colorFor,
      capLabels: capLabels,
      referenceLines: referenceLines,
      applyDefaults: applyDefaults
    };
  }
})();
