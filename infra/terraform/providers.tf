terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Remote state, so terraform apply from CI and from a laptop see the same
  # state and never stomp on each other. Bootstrap the bucket/table once by
  # hand before pointing CI at this.
  backend "s3" {
    bucket         = "fintech-platform-tfstate"
    key            = "global/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "fintech-platform-tf-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "fintech-platform"
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}
