// The screen: three static files in S3, served by CloudFront.
//
// END-TO-END FLOW of one page load:
//
//   1. The browser asks CloudFront for index.html.
//   2. CloudFront fetches it from the S3 bucket over a private connection and
//      caches it. The bucket itself is not public — `aws_cloudfront_origin_access_control`
//      is what lets only this distribution read it.
//   3. The page's JavaScript calls the Lambda Function URL directly. CloudFront
//      is not in that path at all; it only serves the files.
//
// WHY THE BUCKET IS NOT PUBLIC. A public bucket is reachable by its own S3 URL,
// which bypasses CloudFront entirely — no caching, no single front door, and the
// bucket's URL ends up in somebody's bookmark. Origin access control keeps one
// way in.
//
// WHY THE API IS NOT BEHIND CLOUDFRONT TOO. It could be, and that would remove
// the CORS setup. But then every API call pays a CloudFront hop for a response
// that must never be cached, and the distribution becomes something that has to
// be invalidated when the function changes. The page and the API being separate
// origins costs one Function URL setting; see terraform/lambda.
//
// COST. S3 storage for three files under 30 KB is immeasurable. CloudFront is
// free for the first 1 TB out and 10 million requests a month, which this will
// not approach. What this does NOT cover is the Lambda, the EKS control plane
// ($73/month) or the node.
//
// Grep anchor: DDPSRUN-UI

resource "aws_s3_bucket" "ui" {
  bucket        = var.bucket_name
  force_destroy = true // three static files, rebuilt from the repo on every deploy
  tags          = var.tags
}

// Every one of these defaults to "blocked" on a new bucket; stating them makes
// the intent visible in review rather than inherited.
resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

// HOW CLOUDFRONT READS A PRIVATE BUCKET. It signs its own requests with an
// identity the bucket policy below trusts. Nothing else can.
resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = var.bucket_name
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "OnlyThisDistribution"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.ui.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    // Without this condition the statement would let ANY CloudFront
    // distribution in ANY account read the bucket.
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.ui.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = data.aws_iam_policy_document.bucket.json
}

resource "aws_cloudfront_distribution" "ui" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "ddpsrun screen"
  // North America and Europe only. The lab is in one place and the cheaper class
  // is the same service with fewer edge locations.
  price_class = "PriceClass_100"
  tags        = var.tags

  origin {
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_id                = "ui"
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  default_cache_behavior {
    target_origin_id       = "ui"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"

    // AWS's managed CachingOptimized policy. The files are static and every
    // deploy invalidates, so there is nothing to tune here.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    // CloudFront's own *.cloudfront.net certificate. A custom domain would need
    // an ACM certificate in us-east-1, which is a separate decision.
    cloudfront_default_certificate = true
  }
}
