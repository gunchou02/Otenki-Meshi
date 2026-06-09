provider "aws" {
  region = "ap-northeast-1" # 東京リージョン
}

# ==========================================
# 0. 変数定義 (Variables) ★修正: APIキーをコードから分離
# ==========================================
# 値は terraform.tfvars に書く。tfvars は .gitignore 済みなのでGitに上がらない。
variable "weather_api_key" {
  type        = string
  description = "OpenWeatherMap API Key"
  sensitive   = true # ログやplan出力でマスクされる
}

variable "hotpepper_api_key" {
  type        = string
  description = "HotPepper Gourmet API Key"
  sensitive   = true
}

# ==========================================
# 1. DynamoDB (検索ログ保存用)
# ==========================================
resource "aws_dynamodb_table" "otenki_log" {
  name         = "OtenkiMeshi_Log_TF"
  billing_mode = "PAY_PER_REQUEST" # オンデマンド (Free Tier親和性)
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
# 2. IAM Role (Lambda実行権限)
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

# ★修正: 最小権限の原則。コードが実際に使うのは put_item のみ。
#        対象も "*" ではなく、このテーブルのARNに限定する。
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

# ★修正: CloudWatch Logsへの出力はAWS管理ポリシーを利用 (logs:* を自前で書かない)
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ==========================================
# 3. Lambda Function (バックエンドロジック)
# ==========================================
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
  runtime       = "python3.12" # ★修正: 3.9はEOLが近い。サポート期間の長い版へ
  timeout       = 10

  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  # ★修正: APIキーは変数経由。テーブル名もここで注入し、コードの定数を排除。
  environment {
    variables = {
      WEATHER_API_KEY   = var.weather_api_key
      HOTPEPPER_API_KEY = var.hotpepper_api_key
      LOG_TABLE_NAME    = aws_dynamodb_table.otenki_log.name
    }
  }

  # ★修正: GitHub Actions(CI)がコードを更新する運用と共存させる。
  #        terraform apply がCIの更新を巻き戻さないよう、コード差分は無視。
  #        → インフラの真実はTerraform、コードの真実はCI、という住み分け。
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

# ★追加: Lambdaのロググループを明示し、保持期間を設定 (放置するとログが無限に溜まる)
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
    allow_origins = ["*"] # ※本番では CloudFront のドメインに限定するのが望ましい
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
# 5. S3 Bucket (フロントエンド・ホスティング用)
# ==========================================
# ※ NOTE: 本来は S3 を非公開にして CloudFront(OAC) 経由のみ許可するのが現在の定石。
#         今は動作中のデモを壊さないため公開設定のままにしているが、
#         次のステップで CloudFront を Terraform に取り込み、ここを private 化する。
resource "aws_s3_bucket" "frontend" {
  bucket_prefix = "otenki-meshi-website-tf-"
}

resource "aws_s3_bucket_website_configuration" "website" {
  bucket = aws_s3_bucket.frontend.id

  index_document {
    suffix = "index.html"
  }
}

resource "aws_s3_bucket_public_access_block" "public_access" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "public_read" {
  bucket     = aws_s3_bucket.frontend.id
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
# 6. Outputs
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
