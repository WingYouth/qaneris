import os

# Tests opt into the no-op store explicitly. Graph publication behavior is covered
# separately with transactional fakes and opt-in live Neo4j integration tests.
os.environ["SMARTDATA_GRAPH_STORE"] = "null"
