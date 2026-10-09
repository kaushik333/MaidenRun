Empty on purpose: docker-compose.yml mounts this as the backend's ~/.aws when
AWS_CONFIG_DIR isn't set in .env. Set AWS_CONFIG_DIR=~/.aws to use your AWS
CLI profiles instead.
