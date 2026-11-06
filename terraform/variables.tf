variable "name" {
  description = "リソース名の接頭辞"
  type        = string
  default     = "async-compute-demo"
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "image_tag" {
  description = "ECR に push したイメージのタグ"
  type        = string
  default     = "latest"
}

# --- API 層 ---
variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "api_min_tasks" {
  type    = number
  default = 1
}

variable "api_max_tasks" {
  type    = number
  default = 4
}

# --- 計算層 ---
# 1 ワーカーあたりのサイズ。実測したピークメモリに余裕を足して決める。
variable "worker_vcpu" {
  type    = number
  default = 1
}

variable "worker_memory_mib" {
  type    = number
  default = 2048
}

# 同時に動く vCPU の上限。並列度とコストの上限を兼ねる。
variable "batch_max_vcpus" {
  type    = number
  default = 32
}
