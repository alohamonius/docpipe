variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"
}

variable "bedrock_model_id" {
  description = "Bedrock model used by the worker"
  type        = string
  default     = "anthropic.claude-sonnet-4-5"
}

variable "alert_email" {
  description = "Email for CloudWatch alarm notifications (Phase 5)"
  type        = string
  default     = null
}
