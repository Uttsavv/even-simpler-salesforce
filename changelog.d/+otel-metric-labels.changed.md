`OpenTelemetry` metrics no longer carry the `caller` and `sobject` labels, which can take many distinct values; spans still carry them. Pass `metric_attributes=` to choose the metric labels.
