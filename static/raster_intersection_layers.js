(function () {
  const sourceId = "raster-intersection-results";
  const layerId = "raster-intersection-results-point";
  let popup = null;

  function init() {
    if (typeof map === "undefined" || !map.isStyleLoaded()) return;
    if (!map.getSource(sourceId)) {
      map.addSource(sourceId, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });
    }
    if (!map.getLayer(layerId)) {
      map.addLayer({
        id: layerId,
        type: "circle",
        source: sourceId,
        paint: {
          "circle-color": ["coalesce", ["get", "__color"], "#d7191c"],
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            4, 2.5,
            12, 5,
            18, 7
          ],
          "circle-opacity": 0.82,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1
        }
      });
      map.on("click", layerId, showPopup);
      map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
    }
  }

  function render(featureCollection) {
    if (typeof map === "undefined") return;
    if (map.loaded()) {
      init();
      map.getSource(sourceId)?.setData(featureCollection || { type: "FeatureCollection", features: [] });
      return;
    }
    map.once("load", () => render(featureCollection));
  }

  function clear() {
    if (typeof map !== "undefined") {
      map.getSource(sourceId)?.setData({ type: "FeatureCollection", features: [] });
    }
    if (popup) {
      popup.remove();
      popup = null;
    }
  }

  function showPopup(event) {
    const original = event.originalEvent || {};
    if (!original.altKey) return;
    event.preventDefault();

    const feature = event.features && event.features[0];
    if (!feature) return;
    const props = feature.properties || {};
    const rasterValue = formatNumber(props.raster_value);
    const title = props.building_id ? `Building ${props.building_id}` : `Row ${props.exposure_row_id || props.row_id || ""}`;
    const valueLine = props.vector_value
      ? `${props.vector_field || "Vector value"}: ${props.vector_value}`
      : `Raster value: ${rasterValue}`;
    const html = `
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(valueLine)}</span>
    `;
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true })
      .setLngLat(event.lngLat)
      .setHTML(`<div class="raster-result-popup">${html}</div>`)
      .addTo(map);
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "n/a";
    return number.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  if (typeof map !== "undefined") {
    if (map.loaded()) init();
    else map.on("load", init);
  }

  window.rasterIntersectionLayers = { render, clear };
})();
