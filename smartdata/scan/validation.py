from smartdata.scan.contracts import ScanEdgeType, ScanGraph


class ScanGraphValidator:
    def validate(self, graph: ScanGraph) -> None:
        # Pydantic validates identity and referential integrity. Keep policy checks here.
        for edge in graph.edges:
            if edge.type == ScanEdgeType.RELATES_TO:
                if edge.properties.get("confirmed") is not True:
                    raise ValueError("RELATES_TO must be confirmed")
                if edge.properties.get("source") not in {"database", "configuration"}:
                    raise ValueError("RELATES_TO must have a trusted source")
