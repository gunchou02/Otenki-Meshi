provider "aws" {
  region = "ap-northeast-1"
}

# ==========================================
# 0. 変数定義 (Variables)
# ==========================================
variable "weather_api_key" {
  type        = string
  description = "OpenWeatherMap API Key"
  sensitive   = true
}

variable "hotpepper_api_key" {
  type        = string
  description = "HotPepper Gourmet API Key"
  sensitive   = true
}

# ==========================================
# 1. DynamoDB
# ==========================================
resource "aws_dynamodb_table" "otenki_log" {
  name         = "OtenkiMeshi_Log_TF"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"

  attribute {
    name = "request_id"
    type = "S"
  }

  tags = {
    Project   = "OtenkiMeshi"
    ManagedBy = "Terraform"
  }
}

# ==========================================
# 2. IAM Role (Lambda)
# ==========================================
resource "aws_iam_role" "lambda_role" {
  name = "otenki_lambda_role_tf"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_dynamodb_policy" {
  name = "otenki_lambda_dynamodb_policy_tf"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.otenki_log.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ==========================================
# 3. Lambda
# ==========================================
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "../backend"
  output_path = "lambda_function.zip"
}

resource "aws_lambda_function" "backend" {
  filename      = "lambda_function.zip"
  function_name = "OtenkiMeshi_Backend_TF"
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.12"
  timeout       = 10

  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      WEATHER_API_KEY   = var.weather_api_key
      HOTPEPPER_API_KEY = var.hotpepper_api_key
      LOG_TABLE_NAME    = aws_dynamodb_table.otenki_log.name
    }
  }

  # コードはCIが更新する。Terraformはコード差分を無視 (= インフラとコードの責任分離)
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

# ※ NOTE: このロググループはAWSに既存 (Lambdaが過去に自動作成済み)。
#         apply前に必ず terraform import で取り込むこと:
#         terraform import aws_cloudwatch_log_group.lambda_logs /aws/lambda/OtenkiMeshi_Backend_TF
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${aws_lambda_function.backend.function_name}"
  retention_in_days = 14
}

# ==========================================
# 4. API Gateway (HTTP API)
# ==========================================
resource "aws_apigatewayv2_api" "http_api" {
  name          = "OtenkiMeshi_API_TF"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"] # 本番では CloudFront ドメインに絞るのが理想
    allow_methods = ["GET", "OPTIONS"]
    allow_headers = ["Content-Type"]
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http_api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id           = aws_apigatewayv2_api.http_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.backend.invoke_arn
}

resource "aws_apigatewayv2_route" "default_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "GET /recommend"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*"
}

# ==========================================
# 5. S3 Bucket (★ 非公開に戻す / CloudFront OAC からのみ読める)
# ==========================================
resource "aws_s3_bucket" "frontend" {
  bucket_prefix = "otenki-meshi-website-tf-"
}

# ★ 全部 true に戻す = パブリック完全ブロック (定石)
resource "aws_s3_bucket_public_access_block" "public_access" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ★ バケットポリシーは「CloudFrontサービスプリンシパル + この配信からのみ」許可
resource "aws_s3_bucket_policy" "cloudfront_only" {
  bucket = aws_s3_bucket.frontend.id

  # 配信ARNが必要なので明示的に依存させる
  depends_on = [
    aws_cloudfront_distribution.frontend_cdn,
    aws_s3_bucket_public_access_block.public_access,
  ]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontServicePrincipalReadOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.frontend.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.frontend_cdn.arn
          }
        }
      }
    ]
  })
}

# ==========================================
# 6. CloudFront + OAC (★ 復活させる)
# ==========================================
resource "aws_cloudfront_origin_access_control" "frontend_oac" {
  name                              = "otenki-meshi-oac-tf"
  description                       = "OAC for Otenki-Meshi S3 origin"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend_cdn" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "Otenki-Meshi CDN (Terraform-managed)"
  price_class         = "PriceClass_200" # アジア+北米+欧州 (Allより安い)

  origin {
    # ★ OAC使用時は website endpoint ではなく REST endpoint (bucket_regional_domain_name) を指定
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "S3-OtenkiMeshi"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend_oac.id
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "S3-OtenkiMeshi"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400
  }

  # SPA的に深いパスへ来てもindex.htmlを返す (壊れたリンクで黒画面を避ける)
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # *.cloudfront.net のデフォルト証明書
  }

  tags = {
    Project   = "OtenkiMeshi"
    ManagedBy = "Terraform"
  }
}

# ==========================================
# 7. Outputs
# ==========================================
output "api_endpoint" {
  description = "Backend API Endpoint"
  value       = "${aws_apigatewayv2_api.http_api.api_endpoint}/recommend"
}

output "cloudfront_url" {
  description = "Frontend CloudFront URL (HTTPS) ★これを使う"
  value       = "https://${aws_cloudfront_distribution.frontend_cdn.domain_name}"
}

output "s3_bucket_name" {
  description = "S3 Bucket Name (now private, only CloudFront can read)"
  value       = aws_s3_bucket.frontend.id
}
