# module: messaging (Phase 2)

SQS jobs queue + dead-letter queue with redrive policy (maxReceiveCount 3).
Visibility timeout sized to worst-case Bedrock latency × safety factor.
