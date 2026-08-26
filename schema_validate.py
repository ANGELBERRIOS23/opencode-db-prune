"""Schema validation for opencode-db-prune.

Compares the actual database schema against expected structure. Intentionally
brittle — if OpenCode changes anything, the diff flags it. Use --force to
override.
"""

import json
import os

SCHEMA_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_json(filename):
    with open(os.path.join(SCHEMA_DIR, filename)) as f:
        return json.load(f)


def get_expected_tables():
    """Load expected table definitions from JSON."""
    return _load_json("validated_tables.json")


def get_known_extra_tables():
    """Load set of known extra table names from JSON."""
    return set(_load_json("known_extra_tables.json"))


def get_actual_schema(cx):
    """Query the actual schema from a SQLite database.

    Returns dict of {table_name: {"columns": [...], "indexes": [...]}}
    Only returns tables that exist.
    """
    expected = get_expected_tables()
    schema = {}

    tables = cx.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '__drizzle_%'"
    ).fetchall()
    table_names = {t[0] for t in tables}

    for table_name in expected:
        if table_name not in table_names:
            continue

        # Columns: (cid, name, type, notnull, dflt_value, pk)
        cols = cx.execute(f"PRAGMA table_info({table_name})").fetchall()
        columns = [(c[1], c[2].upper(), c[3], c[5]) for c in cols]

        # Indexes
        idx_rows = cx.execute(
            "SELECT name, sql FROM sqlite_master "
            f"WHERE type='index' AND tbl_name='{table_name}' AND sql IS NOT NULL"
        ).fetchall()
        indexes = []
        for idx_name, idx_sql in idx_rows:
            unique = "UNIQUE INDEX" in idx_sql.upper()
            try:
                cols_part = idx_sql.split("ON")[1].split("(")[1].rsplit(")", 1)[0]
                idx_cols = tuple(c.strip().strip('`"') for c in cols_part.split(","))
            except (IndexError, ValueError):
                idx_cols = ()
            indexes.append((idx_name, unique, idx_cols))

        schema[table_name] = {"columns": columns, "indexes": indexes}

    return schema, table_names


def validate_schema(cx, force=False):
    """Compare actual database schema against expected schema.

    Returns (ok, errors, warnings) where:
      - ok: True if schema matches (or forced)
      - errors: list of blocking differences
      - warnings: list of non-blocking observations
    """
    expected = get_expected_tables()
    known_extras = get_known_extra_tables()
    actual, all_tables = get_actual_schema(cx)
    errors = []
    warnings = []

    for table_name, table_def in expected.items():
        # Normalize index columns from JSON lists to tuples
        table_def["indexes"] = [
            [name, unique, tuple(cols)]
            for name, unique, cols in table_def["indexes"]
        ]

        if table_name not in actual:
            errors.append(f"TABLE MISSING: '{table_name}' does not exist in database")
            continue

        actual_cols = actual[table_name]["columns"]
        expected_cols = table_def["columns"]
        extra_allowed = set(table_def.get("extra_columns_allowed", []))
        actual_col_names = [c[0] for c in actual_cols]

        # Check required columns exist
        for col_name, col_type, col_notnull, col_pk in expected_cols:
            if col_name not in actual_col_names:
                errors.append(
                    f"TABLE '{table_name}': column '{col_name}' is missing"
                )
                continue

            actual_col = next(c for c in actual_cols if c[0] == col_name)
            act_name, act_type, act_notnull, act_pk = actual_col

            if act_type != col_type:
                warnings.append(
                    f"TABLE '{table_name}': column '{col_name}' type "
                    f"expected {col_type}, got {act_type}"
                )
            if act_notnull != col_notnull:
                errors.append(
                    f"TABLE '{table_name}': column '{col_name}' NOT NULL "
                    f"expected {col_notnull}, got {act_notnull}"
                )
            if act_pk != col_pk:
                errors.append(
                    f"TABLE '{table_name}': column '{col_name}' PK "
                    f"expected {col_pk}, got {act_pk}"
                )

        # Check for unexpected extra columns
        expected_col_names = [c[0] for c in expected_cols]
        for col_name in actual_col_names:
            if col_name not in expected_col_names and col_name not in extra_allowed:
                errors.append(
                    f"TABLE '{table_name}': unexpected column '{col_name}'"
                )

        # Check indexes
        actual_idx_names = {i[0] for i in actual[table_name]["indexes"]}
        for idx_name, idx_unique, idx_cols in table_def["indexes"]:
            if idx_name not in actual_idx_names:
                warnings.append(
                    f"TABLE '{table_name}': index '{idx_name}' is missing "
                    f"(may not exist on empty tables)"
                )
            else:
                actual_idx = next(
                    i for i in actual[table_name]["indexes"] if i[0] == idx_name
                )
                if actual_idx[1] != idx_unique:
                    errors.append(
                        f"TABLE '{table_name}': index '{idx_name}' uniqueness "
                        f"expected {idx_unique}, got {actual_idx[1]}"
                    )
                if actual_idx[2] != idx_cols:
                    errors.append(
                        f"TABLE '{table_name}': index '{idx_name}' columns "
                        f"expected {idx_cols}, got {actual_idx[2]}"
                    )

    # Report tables present but not in our expected set
    for table_name in all_tables:
        if table_name not in expected and table_name not in known_extras:
            warnings.append(
                f"TABLE '{table_name}' exists but is not in expected schema set"
            )

    ok = len(errors) == 0
    if force and errors:
        print(f"\n  WARNING: --force bypassing {len(errors)} schema error(s)")

    return ok, errors, warnings


def print_schema_report(ok, errors, warnings):
    """Print schema validation results."""
    if ok and not warnings:
        print("  schema    : OK (matches expected)")
        return

    if errors:
        print(f"\n  SCHEMA ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    ✗ {e}")

    if warnings:
        print(f"\n  Schema warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    ⚠ {w}")

    if ok:
        print("\n  schema    : OK (with warnings)")
    else:
        print(f"\n  schema    : FAILED ({len(errors)} blocking error(s))")
