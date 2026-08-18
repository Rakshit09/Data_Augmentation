(function () {
  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || "Raster intersection failed");
    }
    return payload;
  }

  window.rasterIntersectionApi = {
    intersectExposure(payload) {
      return postJson("/api/raster-intersections/exposure", payload);
    },
    intersectDatabase(payload) {
      return postJson("/api/raster-intersections/database", payload);
    },
    async progress(jobId) {
      const response = await fetch(`/api/raster-intersections/progress/${jobId}`);
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || "Could not read raster intersection progress");
      }
      return payload;
    },
    intersectVectorExposure(payload) {
      return postJson("/api/vector-intersections/exposure", payload);
    },
    intersectVectorDatabase(payload) {
      return postJson("/api/vector-intersections/database", payload);
    }
  };
})();
