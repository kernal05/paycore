resource "aws_db_subnet_group" "fintech" {
  name       = "${var.cluster_name}-db-subnets"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.cluster_name}-rds-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db" {
  name = "${var.cluster_name}/postgres-credentials"
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({ username = "fintech", password = random_password.db.result })
}

module "rds" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.9"

  identifier = "${var.cluster_name}-postgres"

  engine               = "postgres"
  engine_version       = "16.4"
  family               = "postgres16"
  major_engine_version = "16"
  instance_class       = var.db_instance_class

  allocated_storage     = 100
  max_allocated_storage = 1000 # storage autoscaling for growth
  storage_encrypted     = true

  db_name  = var.db_name
  username = "fintech"
  password = random_password.db.result
  port     = 5432

  multi_az               = true # synchronous standby in a second AZ; automatic failover
  db_subnet_group_name   = aws_db_subnet_group.fintech.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  backup_retention_period = 30
  backup_window           = "02:00-03:00"
  maintenance_window      = "sun:03:30-sun:04:30"

  deletion_protection = true
  skip_final_snapshot  = false

  performance_insights_enabled = true
  monitoring_interval          = 30

  # A read replica exists purely for the reconciliation/analytics workload,
  # so batch reads never compete with the payment path for connections.
}

resource "aws_db_instance" "read_replica" {
  identifier             = "${var.cluster_name}-postgres-replica"
  replicate_source_db    = module.rds.db_instance_identifier
  instance_class         = var.db_instance_class
  publicly_accessible    = false
  vpc_security_group_ids = [aws_security_group.rds.id]
}
