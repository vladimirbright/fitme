// Fitme stats charts (A§9.1, A§9.3): hand-written inline SVG, vanilla JS, no library. If a
// fetch fails or JS doesn't run at all, the server-rendered table stays visible (nothing here
// removes it unless the corresponding chart actually rendered).

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  return el;
}

function hideFallback(chartId) {
  const table = document.querySelector(`table[data-fallback-for="${chartId}"]`);
  if (table) {
    table.hidden = true;
  }
}

function barChart(container, points, valueKey, labelKey) {
  if (!points.length) {
    return;
  }
  const width = 640;
  const height = 200;
  const padding = 24;
  const max = Math.max(1, ...points.map((p) => p[valueKey]));
  const barWidth = (width - padding * 2) / points.length;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width: "100%",
    role: "img",
    "aria-label": container.dataset.label || "chart",
  });
  points.forEach((point, index) => {
    const barHeight = ((height - padding * 2) * point[valueKey]) / max;
    const x = padding + index * barWidth;
    const y = height - padding - barHeight;
    svg.appendChild(
      svgEl("rect", {
        x: x + 2,
        y,
        width: Math.max(1, barWidth - 4),
        height: barHeight,
        fill: "currentColor",
      })
    );
    const label = svgEl("text", {
      x: x + barWidth / 2,
      y: height - 6,
      "font-size": "9",
      "text-anchor": "middle",
    });
    label.textContent = String(point[labelKey]).slice(5); // MM-DD
    svg.appendChild(label);
  });
  container.appendChild(svg);
}

function lineChart(container, series) {
  const names = Object.keys(series);
  if (!names.length) {
    return;
  }
  const width = 640;
  const height = 220;
  const padding = 28;
  const allPoints = names.flatMap((name) => series[name]);
  const maxKg = Math.max(1, ...allPoints.map((p) => p.kg));
  const dates = [...new Set(allPoints.map((p) => p.date))].sort();
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", role: "img" });
  const colors = ["#1a5fb4", "#c64600", "#2ec27e", "#9141ac", "#e5a50a", "#e01b24"];

  names.forEach((name, seriesIndex) => {
    const points = series[name];
    const path = points
      .map((point) => {
        const x = padding + (dates.indexOf(point.date) / Math.max(1, dates.length - 1)) * (width - padding * 2);
        const y = height - padding - (point.kg / maxKg) * (height - padding * 2);
        return `${x},${y}`;
      })
      .join(" ");
    svg.appendChild(
      svgEl("polyline", {
        points: path,
        fill: "none",
        stroke: colors[seriesIndex % colors.length],
        "stroke-width": "2",
      })
    );
  });

  let legendY = 12;
  names.forEach((name, seriesIndex) => {
    const legend = svgEl("text", {
      x: padding,
      y: legendY,
      "font-size": "10",
      fill: colors[seriesIndex % colors.length],
    });
    legend.textContent = name;
    svg.appendChild(legend);
    legendY += 12;
  });

  container.appendChild(svg);
}

async function fetchJson(url) {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) {
    throw new Error(`${url}: ${response.status}`);
  }
  return response.json();
}

async function main() {
  try {
    const volume = await fetchJson("/api/stats/volume.json");
    const volumeContainer = document.getElementById("chart-volume");
    if (volumeContainer && volume.length) {
      barChart(volumeContainer, volume, "value", "week_start");
      hideFallback("chart-volume");
    }
  } catch (error) {
    console.error("volume chart failed", error);
  }

  try {
    const sessions = await fetchJson("/api/stats/sessions.json");
    const sessionsContainer = document.getElementById("chart-sessions");
    if (sessionsContainer && sessions.length) {
      barChart(sessionsContainer, sessions, "value", "week_start");
      hideFallback("chart-sessions");
    }
  } catch (error) {
    console.error("sessions chart failed", error);
  }

  try {
    const loads = await fetchJson("/api/stats/loads.json");
    const loadsContainer = document.getElementById("chart-loads");
    if (loadsContainer && Object.keys(loads).length) {
      lineChart(loadsContainer, loads);
      hideFallback("chart-loads");
    }
  } catch (error) {
    console.error("loads chart failed", error);
  }
}

main();
