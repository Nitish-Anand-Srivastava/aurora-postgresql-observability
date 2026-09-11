# IAM policies for YACE (yet-another-cloudwatch-exporter)

These documents implement the **least-privilege** permission set needed for the `discovery` jobs in
[`exporters/yace/config.yml`](../exporters/yace/config.yml), which only scrape the `AWS/RDS` CloudWatch
namespace (Aurora PostgreSQL instance- and cluster-level metrics).

## Files

| File | Purpose |
| --- | --- |
| `yace-readonly-policy.json` | Customer-managed IAM policy granting exactly the actions YACE needs. Attach to whatever identity YACE runs as (EC2 instance role, ECS task role, IRSA role, or an assumed cross-account role). |
| `yace-trust-policy-example.json` | **Illustrative only.** Trust policy for a cross-account setup where YACE assumes a role in the account containing the Aurora cluster, per YACE's [`roleArn`](https://github.com/prometheus-community/yet-another-cloudwatch-exporter/blob/master/docs/configuration.md#discovery_job_config) option. Replace the placeholder account ID / external ID before use. |

## Why these actions and no others

Per the authoritative upstream list of required permissions in the YACE README
(<https://github.com/prometheus-community/yet-another-cloudwatch-exporter#authentication>), the **bare
minimum** permissions for static and discovery jobs are:

```text
tag:GetResources
cloudwatch:GetMetricData
cloudwatch:GetMetricStatistics
cloudwatch:ListMetrics
```

- `tag:GetResources` (Resource Groups Tagging API) lets YACE's `discovery` jobs find RDS instance and
  cluster ARNs by tag, instead of you hardcoding resource identifiers.
- `cloudwatch:ListMetrics` / `cloudwatch:GetMetricData` retrieve the actual `AWS/RDS` datapoints. YACE
  uses `GetMetricData` for normal scraping; `GetMetricStatistics` is included for feature parity with
  older/alternate code paths but is not required for the config shipped here.
- `iam:ListAccountAliases` is optional — it only powers the `aws_account_info` label enrichment metric,
  not RDS metric scraping. Remove the `YaceAccountAliasInfoMetricOptional` statement if you don't need it.

None of these actions accept resource-level (`Resource` other than `*`) restriction in IAM — this is an
AWS API limitation, not an oversight: `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, and
`tag:GetResources` are documented as not supporting resource-level permissions
(<https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatch.html>,
<https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsresourcegroupstagging.html>).
Scope is instead enforced by which regions/tags you configure in `exporters/yace/config.yml`
(`searchTags`) and which AWS account/role the credentials belong to.

## Credentials

YACE authenticates using the [AWS SDK default credential chain]
(<https://aws.github.io/aws-sdk-go-v2/docs/configuring-sdk/#specifying-credentials>): environment
variables, shared credentials/config files, an EC2/ECS/EKS instance role, or `AssumeRole`/web identity
federation. **This repository never sets static AWS keys.** In the local Docker Compose example, YACE is
configured to read `AWS_SHARED_CREDENTIALS_FILE` from a bind-mounted, gitignored file that does not exist
by default — see `docker/README.md`.

## Attaching the policy

```bash
aws iam create-policy \
  --policy-name yace-rds-readonly \
  --policy-document file://iam/yace-readonly-policy.json

# Attach to an existing role (EC2 instance role, ECS task role, etc.)
aws iam attach-role-policy \
  --role-name <your-yace-execution-role> \
  --policy-arn arn:aws:iam::<your-account-id>:policy/yace-rds-readonly
```

Do not attach this policy to the Aurora PostgreSQL role or database user — it is an AWS IAM policy for the
YACE process's execution identity, unrelated to the SQL-level `postgres_exporter` role in
`sql/create_monitoring_role.sql`.
