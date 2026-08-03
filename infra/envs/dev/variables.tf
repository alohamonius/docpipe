variable "region" {
  description = "AWS region (us-east-1: DeepSeek-R1 serverless on Bedrock is US-region)"
  type        = string
  default     = "us-east-1"
}

variable "bedrock_model_id" {
  description = "Bedrock model used by the worker (cross-region inference profile ID)"
  type        = string
  default     = "us.deepseek.r1-v1:0"
}

variable "alert_email" {
  description = "Email for CloudWatch alarm notifications (Phase 5)"
  type        = string
  default     = null
}
