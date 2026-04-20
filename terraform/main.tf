provider "aws" {
  region = "ap-northeast-1" # 東京リージョン
}

# ==========================================
# 1. DynamoDB (検索ログ蓄積用・分析基盤)
# ==========================================
resource "aws_dynamodb_table" "otenki_log" {
  name           = "OtenkiMeshi_Log_TF"
  billing_mode   = "PAY_PER_REQUEST" # オンデマンドモード（アクセス数に応じた課金、コスト最適化）
  hash_key       = "request_id"

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
# 2. IAM Role (Lambda実行権限・最小権限の原則)
# ==========================================
resource "aws_iam_role" "lambda_role" {
  name = "otenki_lambda_role_tf"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# DynamoDBへのアクセス権限およびCloudWatch Logs出力権限
resource "aws_iam_role_policy" "lambda_policy" {
  name = "otenki_lambda_policy_tf"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Scan",
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      }
    ]
  })
}

# ==========================================
# 3. Lambda Function (サーバーレス・バックエンド)
# ==========================================
# Pythonコードを自動的にZip圧縮してデプロイ
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "../backend/lambda_function.py"
  output_path = "lambda_function.zip"
}

resource "aws_lambda_function" "backend" {
  filename      = "lambda_function.zip"
  function_name = "OtenkiMeshi_Backend_TF"
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.9"
  timeout       = 10

  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  # 環境変数 (外部APIキー)
  # ※ 本来はAWS Secrets Manager等の利用が推奨されるが、個人開発のため環境変数で代用
  environment {
    variables = {
      WEATHER_API_KEY   = "61522f207af1fd57932c5ae6fedde25a"
      HOTPEPPER_API_KEY = "f1eab82629efc69e"
    }
  }
}

# ==========================================
# 4. API Gateway (フロントエンドからのHTTPリクエスト受付)
# ==========================================
resource "aws_apigatewayv2_api" "http_api" {
  name          = "OtenkiMeshi_API_TF"
  protocol_type = "HTTP"
  
  cors_configuration {
    allow_origins = ["*"] # 本番運用時はCloudFrontのドメインに絞るのが望ましい
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

# API GatewayからLambdaへのリソースベースポリシー(実行許可)
resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*"
}

# ==========================================
# 5. S3 Bucket (フロントエンド・静的アセットホスティング)
# ==========================================
resource "aws_s3_bucket" "frontend" {
  bucket_prefix = "otenki-meshi-website-tf-"
}

# セキュリティ強化: バケットのパブリックアクセスをすべてブロック (CloudFront経由のみ許可するため)
resource "aws_s3_bucket_public_access_block" "public_access" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# バケットポリシー: CloudFront (OAC) からのアクセスのみを許可
resource "aws_s3_bucket_policy" "frontend_policy" {
  bucket     = aws_s3_bucket.frontend.id
  depends_on = [aws_s3_bucket_public_access_block.public_access]

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
# 6. CloudFront (CDN & HTTPS配信・OACによるセキュアな通信)
# ==========================================
resource "aws_cloudfront_origin_access_control" "frontend_oac" {
  name                              = "otenki-meshi-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend_cdn" {
  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "S3-OtenkiMeshi"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend_oac.id
  }

  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "S3-OtenkiMeshi"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }
    
    # ★ GPS機能の有効化に必須: HTTPアクセスをHTTPSへ強制リダイレクト
    viewer_protocol_policy = "redirect-to-https" 
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# ==========================================
# 7. Outputs (インフラ構築結果の出力)
# ==========================================
output "api_endpoint" {
  description = "Backend API Endpoint URL (API Gateway)"
  value       = "${aws_apigatewayv2_api.http_api.api_endpoint}/recommend"
}

output "s3_bucket_name" {
  description = "S3 Bucket Name (For GitHub Actions deployment)"
  value       = aws_s3_bucket.frontend.id
}

output "cloudfront_url" {
  description = "🔥 Frontend App URL (HTTPS Enabled - Use this link for GPS!)"
  value       = "https://${aws_cloudfront_distribution.frontend_cdn.domain_name}"
}
