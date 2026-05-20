WITH exposure AS (
    SELECT
        ROW_NUMBER() OVER (ORDER BY 1) AS __exposure_row_id,
        {{ exposure_columns }},
        TRY_CAST({{ lon_column }} AS DOUBLE) AS __lon,
        TRY_CAST({{ lat_column }} AS DOUBLE) AS __lat,
        TRY_CAST({{ lon_column }} AS DOUBLE) BETWEEN -180 AND 180
            AND TRY_CAST({{ lat_column }} AS DOUBLE) BETWEEN -90 AND 90 AS __valid_coordinates,
        ST_MAKEPOINT(TRY_CAST({{ lon_column }} AS DOUBLE), TRY_CAST({{ lat_column }} AS DOUBLE)) AS __pt,
        {{ radius_m }}::DOUBLE / 111320.0 AS __lat_delta,
        {{ radius_m }}::DOUBLE / (
            111320.0 * GREATEST(COS(RADIANS(TRY_CAST({{ lat_column }} AS DOUBLE))), 0.2)
        ) AS __lon_delta
    FROM {{ exposure_table }}
),
inside_ranked AS (
    SELECT
        e.__exposure_row_id,
        ROW_NUMBER() OVER (
            PARTITION BY e.__exposure_row_id
            ORDER BY b.footprint_area_m2 ASC NULLS LAST, b.building_id
        ) AS rn,
        {{ raw_building_columns }}
    FROM exposure e
    JOIN {{ buildings_table }} b
        ON e.__valid_coordinates
        AND '{{ mode }}' IN ('inside', 'inside_nearest')
        AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
        AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
        AND ST_INTERSECTS(b.geom, e.__pt)
),
inside_matches AS (
    SELECT *
    FROM inside_ranked
    WHERE rn = 1
),
nearest_ranked AS (
    SELECT
        e.__exposure_row_id,
        CASE
            WHEN '{{ mode }}' = 'centroid'
                THEN ST_DISTANCE(ST_MAKEPOINT(b.centroid_lon, b.centroid_lat), e.__pt)
            ELSE ST_DISTANCE(b.geom, e.__pt)
        END::DOUBLE AS distance_m,
        ROW_NUMBER() OVER (
            PARTITION BY e.__exposure_row_id
            ORDER BY
                CASE
                    WHEN '{{ mode }}' = 'centroid'
                        THEN ST_DISTANCE(ST_MAKEPOINT(b.centroid_lon, b.centroid_lat), e.__pt)
                    ELSE ST_DISTANCE(b.geom, e.__pt)
                END,
                b.footprint_area_m2 ASC NULLS LAST
        ) AS rn,
        {{ raw_building_columns }}
    FROM exposure e
    LEFT JOIN inside_matches i
        ON i.__exposure_row_id = e.__exposure_row_id
    JOIN {{ buildings_table }} b
        ON e.__valid_coordinates
        AND (
            (
                '{{ mode }}' = 'centroid'
                AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
                AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta
                AND ST_DWITHIN(ST_MAKEPOINT(b.centroid_lon, b.centroid_lat), e.__pt, {{ radius_m }}::DOUBLE)
            )
            OR (
                '{{ mode }}' = 'inside_nearest'
                AND i.__exposure_row_id IS NULL
                AND b.bbox_xmin <= e.__lon + e.__lon_delta
                AND b.bbox_xmax >= e.__lon - e.__lon_delta
                AND b.bbox_ymin <= e.__lat + e.__lat_delta
                AND b.bbox_ymax >= e.__lat - e.__lat_delta
                AND ST_DWITHIN(b.geom, e.__pt, {{ radius_m }}::DOUBLE)
            )
        )
),
nearest_matches AS (
    SELECT *
    FROM nearest_ranked
    WHERE rn = 1
)
SELECT
    {{ output_exposure_columns }},
    e.__valid_coordinates AS "coordinate_valid",
    CASE
        WHEN i.__exposure_row_id IS NOT NULL THEN 'inside_polygon'
        WHEN n.__exposure_row_id IS NOT NULL AND '{{ mode }}' = 'centroid' THEN 'nearest_centroid'
        WHEN n.__exposure_row_id IS NOT NULL THEN 'nearest_polygon'
        ELSE 'none'
    END AS "building_match_type",
    CASE
        WHEN i.__exposure_row_id IS NOT NULL THEN 0.0
        ELSE n.distance_m
    END AS "building_distance_m",
    CASE
        WHEN i.__exposure_row_id IS NOT NULL THEN 'high'
        WHEN n.__exposure_row_id IS NULL THEN 'none'
        WHEN n.distance_m <= 15 THEN 'medium'
        ELSE 'low'
    END AS "building_confidence",
    {{ coalesced_building_columns }}
FROM exposure e
LEFT JOIN inside_matches i
    ON i.__exposure_row_id = e.__exposure_row_id
LEFT JOIN nearest_matches n
    ON n.__exposure_row_id = e.__exposure_row_id
ORDER BY e.__exposure_row_id;
