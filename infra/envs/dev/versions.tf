terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    # Bucket/key/region supplied via backend.hcl (gitignored):
    #   terraform init -backend-config=backend.hcl
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "docpipe"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}
