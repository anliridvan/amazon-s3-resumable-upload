from aws_cdk import core
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_autoscaling as autoscaling
import aws_cdk.aws_iam as iam
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_cloudwatch as cw
import aws_cdk.aws_cloudwatch_actions as action
import aws_cdk.aws_sns as sns
import aws_cdk.aws_sns_subscriptions as sub
import aws_cdk.aws_logs as logs

# Adjust ec2 type here
worker_type = "c5.large"
jobsender_type = "t3.micro"

# Setup your alarm email
alarm_email = "alarm_your_email@email.com"

# Specify your AMI here
linux_ami = ec2.AmazonLinuxImage(generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2,
                                 edition=ec2.AmazonLinuxEdition.STANDARD,
                                 virtualization=ec2.AmazonLinuxVirt.HVM,
                                 storage=ec2.AmazonLinuxStorage.GENERAL_PURPOSE
                                 )
# Load your user data for ec2
with open("./cdk/user_data_worker.sh") as f:
    user_data_worker = f.read()
with open("./cdk/user_data_jobsender.sh") as f:
    user_data_jobsender = f.read()


class CdkEc2Stack(core.Stack):

    def __init__(self, scope: core.Construct, _id: str, vpc, bucket_para,
                 # key_name,
                 ddb_file_list, sqs_queue, sqs_queue_DLQ, ssm_bucket_para, ssm_credential_para,
                 # s3bucket,
                 **kwargs) -> None:
        super().__init__(scope, _id, **kwargs)

        # Create jobsender ec2 node
        jobsender = ec2.Instance(self, "jobsender",
                                 instance_name="s3_migrate_cluster_jobsender",
                                 instance_type=ec2.InstanceType(
                                     instance_type_identifier=jobsender_type),
                                 machine_image=linux_ami,
                                 # key_name=key_name,
                                 user_data=ec2.UserData.custom(user_data_jobsender),
                                 vpc=vpc,
                                 vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC))

        # jobsender.connections.allow_from_any_ipv4(ec2.Port.tcp(22), "Internet access SSH")
        # Don't need SSH since we use Session Manager

        # Assign EC2 Policy to use SSM and CWAgent
        jobsender.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"))
        jobsender.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"))

        # jobsender.role.add_managed_policy(
        #     iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FullAccess"))
        # Don't give full access s3 to ec2, violate security rule

        # Create Autoscaling Group with fixed 2*EC2 hosts
        worker_asg = autoscaling.AutoScalingGroup(self, "worker-asg",
                                                  vpc=vpc,
                                                  vpc_subnets=ec2.SubnetSelection(
                                                      subnet_type=ec2.SubnetType.PUBLIC),
                                                  instance_type=ec2.InstanceType(
                                                      instance_type_identifier=worker_type),
                                                  machine_image=linux_ami,
                                                  # key_name=key_name,  # Optional if use SSM-SessionManager
                                                  user_data=ec2.UserData.custom(user_data_worker),
                                                  desired_capacity=1,
                                                  min_capacity=1,
                                                  max_capacity=10,
                                                  spot_price="0.5"
                                                  )

        # TODO: There is no MetricsCollection in CDK autoscaling group high level API yet.
        # You need to enable "Group Metrics Collection" in EC2 Console Autoscaling Group - Monitoring tab for metric:
        # GroupDesiredCapacity, GroupInServiceInstances, GroupPendingInstances and etc.

        # worker_asg.connections.allow_from_any_ipv4(ec2.Port.tcp(22), "Internet access SSH")
        # Don't need SSH since we use Session Manager

        # Assign EC2 Policy to use SSM and CWAgent
        worker_asg.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"))
        worker_asg.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"))

        # Allow EC2 access new DynamoDB Table
        ddb_file_list.grant_full_access(jobsender)
        ddb_file_list.grant_full_access(worker_asg)

        # Allow EC2 access new sqs and its DLQ
        sqs_queue.grant_consume_messages(jobsender)
        sqs_queue.grant_send_messages(jobsender)
        sqs_queue.grant_consume_messages(worker_asg)
        sqs_queue_DLQ.grant_consume_messages(jobsender)

        # Allow EC2 access SSM Parameter Store, get bucket infor and get credential
        ssm_bucket_para.grant_read(jobsender)
        ssm_credential_para.grant_read(jobsender)
        ssm_credential_para.grant_read(worker_asg)

        # Allow EC2 access new s3 bucket
        # s3bucket.grant_read(jobsender)
        # s3bucket.grant_read(worker_asg)

        # Allow EC2 access exist s3 bucket
        bucket_name = ''
        for b in bucket_para:
            if bucket_name != b['src_bucket']:  # 如果列了多个相同的Bucket，就跳过
                bucket_name = b['src_bucket']
                s3exist_bucket = s3.Bucket.from_bucket_name(self,
                                                            bucket_name,  # 用这个做id
                                                            bucket_name=bucket_name)
                s3exist_bucket.grant_read(jobsender)
                s3exist_bucket.grant_read(worker_asg)

        # Dashboard to monitor SQS and EC2
        board = cw.Dashboard(self, "s3_migrate", dashboard_name="s3_migrate_cluster")

        ec2_metric_net = cw.Metric(namespace="AWS/EC2",
                                   metric_name="NetworkOut",
                                   # dimensions={"AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                   period=core.Duration.minutes(1),
                                   statistic="Sum")
        ec2_metric_cpu_max = cw.Metric(namespace="AWS/EC2",
                                       metric_name="CPUUtilization",
                                       # dimensions={"AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                       period=core.Duration.minutes(1),
                                       statistic="Maximum")
        ec2_metric_cpu_avg = cw.Metric(namespace="AWS/EC2",
                                       metric_name="CPUUtilization",
                                       # dimensions={"AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                       period=core.Duration.minutes(1))

        autoscaling_GroupDesiredCapacity = cw.Metric(namespace="AWS/AutoScaling",
                                                     metric_name="GroupDesiredCapacity",
                                                     dimensions={
                                                         "AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                                     period=core.Duration.minutes(1))
        autoscaling_GroupInServiceInstances = cw.Metric(namespace="AWS/AutoScaling",
                                                        metric_name="GroupInServiceInstances",
                                                        dimensions={
                                                            "AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                                        period=core.Duration.minutes(1))
        autoscaling_GroupMinSize = cw.Metric(namespace="AWS/AutoScaling",
                                             metric_name="GroupMinSize",
                                             dimensions={
                                                 "AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                             period=core.Duration.minutes(1))
        autoscaling_GroupMaxSize = cw.Metric(namespace="AWS/AutoScaling",
                                             metric_name="GroupMaxSize",
                                             dimensions={
                                                 "AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                             period=core.Duration.minutes(1))
        # CWAgent collected metric
        cwagent_mem_avg = cw.Metric(namespace="CWAgent",
                                    metric_name="mem_used_percent",
                                    dimensions={"AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                    statistic="Average",
                                    period=core.Duration.minutes(1))
        cwagent_mem_max = cw.Metric(namespace="CWAgent",
                                    metric_name="mem_used_percent",
                                    dimensions={"AutoScalingGroupName": worker_asg.auto_scaling_group_name},
                                    statistic="Maximum",
                                    period=core.Duration.minutes(1))

        # CWAgent collected application logs - filter metric
        s3_migrate_log = logs.LogGroup(self, "applog",
                                       log_group_name="s3_migration_log")
        s3_migrate_log.add_metric_filter("ERROR",
                                         metric_name="ERROR-Logs",
                                         metric_namespace="s3_migrate",
                                         metric_value="1",
                                         filter_pattern=logs.FilterPattern.literal(
                                             '"ERROR"'))
        s3_migrate_log.add_metric_filter("WARNING",
                                         metric_name="WARNING-Logs",
                                         metric_namespace="s3_migrate",
                                         metric_value="1",
                                         filter_pattern=logs.FilterPattern.literal(
                                             '"WARNING"'))
        log_metric_ERROR = cw.Metric(namespace="s3_migrate",
                                     metric_name="ERROR-Logs",
                                     statistic="Sum",
                                     period=core.Duration.minutes(1))
        log_metric_WARNING = cw.Metric(namespace="s3_migrate",
                                       metric_name="WARNING-Logs",
                                       statistic="Sum",
                                       period=core.Duration.minutes(1))

        board.add_widgets(cw.GraphWidget(title="EC2-ALL-NETWORK",
                                         left=[ec2_metric_net]),
                          cw.GraphWidget(title="EC2-ALL-CPU",
                                         left=[ec2_metric_cpu_avg, ec2_metric_cpu_max]),
                          cw.GraphWidget(title="EC2-AutoscalingGroup-MEMORY",
                                         left=[cwagent_mem_max, cwagent_mem_avg]),
                          cw.SingleValueWidget(title="EC2-AutoscalingGroup-Capacity",
                                               metrics=[autoscaling_GroupDesiredCapacity,
                                                        autoscaling_GroupInServiceInstances,
                                                        autoscaling_GroupMinSize,
                                                        autoscaling_GroupMaxSize],
                                               height=6),
                          )

        board.add_widgets(cw.GraphWidget(title="SQS-Jobs",
                                         left=[sqs_queue.metric_approximate_number_of_messages_visible(
                                             period=core.Duration.minutes(1)
                                         ),
                                               sqs_queue.metric_approximate_number_of_messages_not_visible(
                                                   period=core.Duration.minutes(1)
                                               )]),
                          cw.GraphWidget(title="SQS-DeadLetterQueue",
                                         left=[sqs_queue_DLQ.metric_approximate_number_of_messages_visible(
                                             period=core.Duration.minutes(1)
                                         ),
                                               sqs_queue_DLQ.metric_approximate_number_of_messages_not_visible(
                                                   period=core.Duration.minutes(1)
                                               )]),
                          cw.GraphWidget(title="ERROR/WARNING Logs",
                                         left=[log_metric_ERROR],
                                         right=[log_metric_WARNING],
                                         height=6),
                          cw.SingleValueWidget(title="Running/Waiting and Death Jobs",
                                               metrics=[sqs_queue.metric_approximate_number_of_messages_not_visible(
                                                   period=core.Duration.minutes(1)
                                               ),
                                                        sqs_queue.metric_approximate_number_of_messages_visible(
                                                            period=core.Duration.minutes(1)
                                                        ),
                                                        sqs_queue_DLQ.metric_approximate_number_of_messages_not_visible(
                                                            period=core.Duration.minutes(1)
                                                        ),
                                                        sqs_queue_DLQ.metric_approximate_number_of_messages_visible(
                                                            period=core.Duration.minutes(1)
                                                        )
                                                        ],
                                               height=6)
                          )

        # Autoscaling up when visible message > 100 every 3 of 3 x 5 mins
        worker_asg.scale_on_metric("scaleup", metric=sqs_queue.metric_approximate_number_of_messages_visible(),
                                   scaling_steps=[autoscaling.ScalingInterval(
                                       change=1,
                                       lower=100,
                                       upper=500
                                   ), autoscaling.ScalingInterval(
                                       change=2,
                                       lower=500
                                   ),
                                       autoscaling.ScalingInterval(
                                           change=0,
                                           upper=100,
                                           lower=0
                                       )],
                                   adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY)

        # Alarm for queue empty and ec2 > 1
        # 消息队列空（没有Visible+Invisible），并且EC2不止一台，则告警，并设置EC2为1台
        # 这里还可以根据场景调整，如果Jobsender也用来做传输，则可以在这里设置没有任务的时候，Autoscaling Group为0
        metric_all_message = cw.MathExpression(
            expression="IF(((a+b) == 0) AND (c >1), 0, 1)",  # a+b且c>1则设置为0，告警
            label="empty_queue_expression",
            using_metrics={
                "a": sqs_queue.metric_approximate_number_of_messages_visible(),
                "b": sqs_queue.metric_approximate_number_of_messages_not_visible(),
                "c": autoscaling_GroupInServiceInstances
            }
        )
        alarm_0 = cw.Alarm(self, "SQSempty",
                           alarm_name="s3-migration-cluster-SQS queue empty and ec2 more than 1 in Cluster",
                           metric=metric_all_message,
                           threshold=0,
                           comparison_operator=cw.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
                           evaluation_periods=3,
                           datapoints_to_alarm=3,
                           treat_missing_data=cw.TreatMissingData.NOT_BREACHING
                           )
        alarm_topic_empty = sns.Topic(self, "SQS queue empty and ec2 more than 1 in Cluster")
        # 这个告警可以作为批量传输完成后的通知，而且这样做可以只通知一次，而不会不停地通知
        alarm_topic_empty.add_subscription(subscription=sub.EmailSubscription(alarm_email))
        alarm_0.add_alarm_action(action.SnsAction(alarm_topic_empty))

        # If queue empty, set autoscale down to 1 EC2
        action_shutdown = autoscaling.StepScalingAction(self, "shutdown",
                                                        auto_scaling_group=worker_asg,
                                                        adjustment_type=autoscaling.AdjustmentType.EXACT_CAPACITY
                                                        )
        action_shutdown.add_adjustment(adjustment=1, upper_bound=0)
        alarm_0.add_alarm_action(action.AutoScalingAction(action_shutdown))

        # While message in SQS-DLQ, alarm to sns
        alarm_DLQ = cw.Alarm(self, "SQS_DLQ",
                             alarm_name="s3-migration-cluster-SQS DLQ more than 1 message-Cluster",
                             metric=sqs_queue_DLQ.metric_approximate_number_of_messages_visible(),
                             threshold=0,
                             comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                             evaluation_periods=3,
                             datapoints_to_alarm=3,
                             treat_missing_data=cw.TreatMissingData.IGNORE)
        alarm_topic_DLQ = sns.Topic(self, "SQS DLQ more than 1 message-Cluster")
        alarm_topic_DLQ.add_subscription(subscription=sub.EmailSubscription(alarm_email))
        alarm_DLQ.add_alarm_action(action.SnsAction(alarm_topic_DLQ))

        # Output
        core.CfnOutput(self, "JobSenderEC2", value=jobsender.instance_id)
        core.CfnOutput(self, "WorkerEC2AutoscalingGroup", value=worker_asg.auto_scaling_group_name)
        core.CfnOutput(self, "Dashboard", value="CloudWatch Dashboard name s3_migrate_cluster")
        core.CfnOutput(self, "Alarm", value="CloudWatch SQS queue empty Alarm for cluster: " + alarm_email)
