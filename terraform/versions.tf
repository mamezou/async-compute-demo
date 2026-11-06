terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
  # 実運用では state を S3 に置く。デモはローカル state のまま。
  # backend "s3" {
  #   bucket       = "my-tfstate-bucket"
  #   key          = "async-compute-demo/terraform.tfstate"
  #   region       = "ap-northeast-1"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}
