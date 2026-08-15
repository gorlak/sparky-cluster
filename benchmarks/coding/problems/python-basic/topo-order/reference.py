def topo_order(graph):
    nodes = set(graph) | {dep for deps in graph.values() for dep in deps}
    pending = {n: set(graph.get(n, ())) for n in nodes}
    out = []
    while pending:
        ready = sorted(n for n, deps in pending.items() if not deps)
        if not ready:
            raise ValueError(f"cycle among {sorted(pending)}")
        for node in ready:
            out.append(node)
            del pending[node]
        for deps in pending.values():
            deps.difference_update(ready)
    return out
