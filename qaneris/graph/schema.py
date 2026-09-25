NODE_CONSTRAINTS = tuple(
    f"CREATE CONSTRAINT qaneris_{label.lower()}_id IF NOT EXISTS "
    f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
    for label in ("Database", "Namespace", "DataObject", "Field", "Index", "Constraint")
)

GRAPH_INDEXES = (
    (
        "CREATE INDEX qaneris_object_datasource IF NOT EXISTS "
        "FOR (n:DataObject) ON (n.datasource_id)"
    ),
    (
        "CREATE INDEX qaneris_database_workspace IF NOT EXISTS "
        "FOR (n:Database) ON (n.workspace_id)"
    ),
)
