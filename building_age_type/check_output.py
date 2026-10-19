"""Print summary tables of an estimate_age_type.py output database."""
import sys

import duckdb

con = duckdb.connect(sys.argv[1], read_only=True)
q = lambda s: print(con.execute(s).fetchdf().to_string(), "\n")
q("SELECT * EXCLUDE (config) FROM build_info")
q("SELECT * FROM validation ORDER BY label_source, region")
q("SELECT nuts2, year_built_source, count(*) n, round(avg(construction_period_prob), 2) p FROM buildings GROUP BY ALL ORDER BY 1, 2")
q("SELECT nuts2, construction_period, count(*) n FROM buildings GROUP BY ALL ORDER BY 1, 2")
q("SELECT nuts2, construction_material, count(*) n, round(avg(construction_material_prob), 2) p FROM buildings GROUP BY ALL ORDER BY 1, 3 DESC")
q("""SELECT nuts2, structural_type_source, structural_type, count(*) n, round(avg(structural_type_prob), 2) p,
        round(avg(structural_type_regional_share), 2) AS regional_share FROM buildings GROUP BY ALL ORDER BY 1, 2, 4 DESC""")
