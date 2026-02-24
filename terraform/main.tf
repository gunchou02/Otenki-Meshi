provider "aws" {
  region = "ap-northeast-1" # 東京リージョン
}

# ==========================================
# 1. DynamoDB (検索ログ保存用)
# ==========================================
resource "aws_dynamodb_table" "otenki_log" {
  name           = "OtenkiMeshi_Log_TF"  # 既存のリソースと区別するため _TF を付与
  billing_mode   = "PAY_PER_REQUEST"     # オンデマンドモード (リクエストごとの課金 / Free Tier親和性)
  hash_key       = "request_id"

  attribute {
    name = "request_id"
    type = "S"
  }

  tags = {
    Project = "OtenkiMeshi"
    ManagedBy = "Terraform"
  }
}

# ==========================================
# 2. IAM Role (Lambda実行権限)
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
# 3. Lambda Function (バックエンドロジック)
# ==========================================
# Pythonコードを自動的にZip圧縮
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "../backend/lambda_function.py"
  output_path = "lambda_function.zip"
}

resource "aws_lambda_function" "backend" {
  filename      = "lambda_function.zip"
  function_name = "OtenkiMeshi_Backend_TF" # 既存のリソースと区別
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.9"
  timeout       = 10

  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  # 環境変数設定 (APIキー)
  # ※ 本番環境ではAWS Secrets ManagerやParameter Storeの利用を推奨
  environment {
    variables = {
      WEATHER_API_KEY   = "61522f207af1fd57932c5ae6fedde25a"
      HOTPEPPER_API_KEY = "f1eab82629efc69e"
    }
  }
}

# ==========================================
# 4. API Gateway (HTTP API)
# ==========================================
resource "aws_apigatewayv2_api" "http_api" {
  name          = "OtenkiMeshi_API_TF"
  protocol_type = "HTTP"
  
  cors_configuration {
    allow_origins = ["*"]
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

# API GatewayからのLambda実行を許可
resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*"
}

# ==========================================
# 5. S3 Bucket (フロントエンド・ホスティング用)
# ==========================================
resource "aws_s3_bucket" "frontend" {
  bucket_prefix = "otenki-meshi-website-tf-" # ユニークなバケット名を生成
}

resource "aws_s3_bucket_website_configuration" "website" {
  bucket = aws_s3_bucket.frontend.id

  index_document {
    suffix = "index.html"
  }
}

# パブリックアクセス許可 (静的ウェブサイトホスティングのため)
resource "aws_s3_bucket_public_access_block" "public_access" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "public_read" {
  bucket = aws_s3_bucket.frontend.id
  depends_on = [aws_s3_bucket_public_access_block.public_access]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.frontend.arn}/*"
      }
    ]
  })
}

# ==========================================
# 6. Outputs (デプロイ後のエンドポイント出力)
# ==========================================
output "api_endpoint" {
  description = "Backend API Endpoint URL"
  value       = "${aws_apigatewayv2_api.http_api.api_endpoint}/recommend"
}

output "website_url" {
  description = "Frontend S3 Website URL"
  value       = aws_s3_bucket_website_configuration.website.website_endpoint
}

output "s3_bucket_name" {
  description = "Created S3 Bucket Name"
  value       = aws_s3_bucket.frontend.id
}