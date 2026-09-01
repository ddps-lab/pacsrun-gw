variable "region" {
  description = "Where the bucket lives. CloudFront is global and reads it from anywhere."
  type        = string
  default     = "us-west-2"
}

variable "bucket_name" {
  description = <<-EOT
    Bucket for the screen's files. S3 bucket names are globally unique across all
    AWS accounts, so this normally carries the account id.
  EOT
  type        = string
}

variable "tags" {
  description = "Tags applied to every object this module creates."
  type        = map(string)
  default = {
    Project   = "pacsrun-gw"
    ManagedBy = "terraform"
  }
}
