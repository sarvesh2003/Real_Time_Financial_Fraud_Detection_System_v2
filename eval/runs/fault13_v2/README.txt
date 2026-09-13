v2 tool-docs experiment, INVALID - infrastructure failure, not a result.

Both legs completed all 15 tool calls, then the Kaggle ngrok tunnel went
offline (ERR_NGROK_3200) during conclude(). rag=off recorded PARSE_FAILURE;
rag=on crashed with OpenAIModelNotFoundError. Neither produced a diagnosis.

Not graded and not counted as abstentions - scoring a dead endpoint as the
agent declining to answer is exactly the misattribution the empty-response
retry in agent/graph.py exists to prevent.

The v2 arm was abandoned here. Faults 13 and 6 were never completed under v2
docs, so no v1-vs-v2 comparison is claimed.
