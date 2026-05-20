WITH params AS (
    SELECT
        %s::DOUBLE AS lon,
        %s::DOUBLE AS lat,
        %s::DOUBLE AS radius_m,
        ST_MAKEPOINT(%s::DOUBLE, %s::DOUBLE) AS pt
),
inside_match AS (
    SELECT
        'inside_polygon' AS "match_type",
        0.0::DOUBLE AS "distance_m",
        'high' AS "confidence",
        {{ building_columns }},
        ST_ASGEOJSON(b.geom) AS "geometry"
    FROM {{ buildings_table }} b
    CROSS JOIN params p
    WHERE
        p.lon BETWEEN b.bbox_xmin AND b.bbox_xmax
        AND p.lat BETWEEN b.bbox_ymin AND b.bbox_ymax
        AND ST_INTERSECTS(b.geom, p.pt)
    QUALIFY ROW_NUMBER() OVER (
        ORDER BY b.footprint_area_m2 ASC NULLS LAST, b.building_id
    ) = 1
),
nearest_match AS (
    SELECT
        'nearest_polygon' AS "match_type",
        ST_DISTANCE(b.geom, p.pt)::DOUBLE AS "distance_m",
        CASE
            WHEN ST_DISTANCE(b.geom, p.pt) <= 15 THEN 'medium'
            ELSE 'low'
        END AS "confidence",
        {{ building_columns }},
        ST_ASGEOJSON(b.geom) AS "geometry"
    FROM {{ buildings_table }} b
    CROSS JOIN params p
    WHERE
        NOT EXISTS (SELECT 1 FROM inside_match)
        AND b.bbox_xmin <= p.lon + (
            p.radius_m / (111320.0 * GREATEST(COS(RADIANS(p.lat)), 0.2))
        )
        AND b.bbox_xmax >= p.lon - (
            p.radius_m / (111320.0 * GREATEST(COS(RADIANS(p.lat)), 0.2))
        )
        AND b.bbox_ymin <= p.lat + (p.radius_m / 111320.0)
        AND b.bbox_ymax >= p.lat - (p.radius_m / 111320.0)
        AND ST_DWITHIN(b.geom, p.pt, p.radius_m)
    QUALIFY ROW_NUMBER() OVER (
        ORDER BY ST_DISTANCE(b.geom, p.pt), b.footprint_area_m2 ASC NULLS LAST
    ) = 1
)
SELECT *
FROM inside_match
UNION ALL
SELECT *
FROM nearest_match
LIMIT 1;
