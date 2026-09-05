output "url" {
  description = "The address a researcher opens. Also what goes in the Function URL's allowed origins."
  value       = "https://${aws_cloudfront_distribution.ui.domain_name}"
}

output "bucket" {
  description = "Set as the UI_BUCKET repository variable so CI can upload the files."
  value       = aws_s3_bucket.ui.id
}

output "distribution_id" {
  description = "Set as the UI_DISTRIBUTION repository variable. CI invalidates the cache with it."
  value       = aws_cloudfront_distribution.ui.id
}

output "next_steps" {
  description = "The two things that have to happen after apply."
  value       = <<-EOT
    # 1. Let the browser call the API. The page and the Function URL are different
    #    origins, so without this every request the page makes is refused by the
    #    browser before it leaves the machine.
    cd ../lambda && terraform apply \
      -var 'cors_allow_origins=["https://${aws_cloudfront_distribution.ui.domain_name}"]'

    # 2. Let CI publish the files.
    gh variable set UI_BUCKET       --repo ddps-lab/pacsrun-gw --body ${aws_s3_bucket.ui.id}
    gh variable set UI_DISTRIBUTION --repo ddps-lab/pacsrun-gw --body ${aws_cloudfront_distribution.ui.id}
  EOT
}
