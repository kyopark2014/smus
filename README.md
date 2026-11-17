# SageMaker Unified Studio의 활용

## Amazon SageMaker Unified Studio란?

**Amazon SageMaker Unified Studio**는 데이터 분석, 머신러닝(ML), 생성형 AI 작업을 하나의 환경에서 수행할 수 있게 해주는 AWS의 통합 개발 환경입니다. 차세대 Amazon SageMaker의 핵심 워크스페이스로, 조직의 데이터를 찾고 활용하며 SQL·데이터 처리·모델 학습·생성형 AI 앱까지 한곳에서 협업·구축·공유할 수 있습니다.

### 통합하는 AWS 서비스

| 영역 | 서비스 |
|------|--------|
| 데이터 처리 | Amazon EMR, AWS Glue, Amazon Athena |
| SQL·웨어하우스 | Amazon Redshift |
| 머신러닝 | Amazon SageMaker AI |
| 생성형 AI | Amazon Bedrock |
| 워크플로 | Amazon MWAA 등 |

### 참고 자료

- [What is Amazon SageMaker Unified Studio?](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/adminguide/what-is-sagemaker-unified-studio.html)
- [Generative AI assistance (Amazon Q / Data Agent)](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/adminguide/amazonq.html)
- [Cost attribution (Studio admin best practices)](https://docs.aws.amazon.com/whitepapers/latest/sagemaker-studio-admin-best-practices/cost-attribution.html)

## 도메인 설치 설정

| 항목 | 값 |
|------|-----|
| Region | `ap-northeast-1` (Tokyo) — IdC primary와 동일 |
| Domain 이름 | `smus-kr` |
| Domain version | `V2` (Unified Studio) |
| 인증 | IAM Identity Center (IdC) — installer에서 SSO 설정 |
| VPC | 신규 생성 (프라이빗 서브넷 중심, NAT 최소) |
| Bedrock | **PrivateLink** (`bedrock-runtime` 등 VPC endpoint) |
| Direct Connect | **미사용** |
| JupyterLab Space 인스턴스 | `ml.t3.medium`, `ml.t3.large`, `ml.t3.xlarge` 만 허용 (**IAM Deny**) |
| Notebooks 인스턴스 | 정책 의도: `sc.t3.*` 만 허용 — **자동 차단 불가**, **감지·SNS 알람**만 |
| SageMaker Data Agent | 비활성화 (IdC 토글 + IAM Deny) |
| 모니터링 | Lambda + DynamoDB + CloudWatch Dashboard (`dashboard.py`) + Notebooks **감지·알람** |

## 실행 방법

```bash
cd smus
python installer.py --idc-admin-email admin@example.com
# installer 마지막에 dashboard.py 를 호출합니다.
python dashboard.py          # 대시보드만 재생성
python uninstaller.py        # 정리 (주의)
```

생성 리소스는 `config.json`에 저장됩니다.

### IdC

Organization IdC primary가 **Tokyo (`ap-northeast-1`)** 이므로 SMUS도 동일 리전에 배포합니다.  
(`installer.py`가 IdC 인스턴스를 resolve 해 도메인 SSO에 연결)

### 기본 프로젝트 프로필 (`installer.py`에 포함)

`installer.py`가 도메인 생성 후 자동으로 수행합니다 (`--skip-project-profile`로 생략 가능).

- **Tooling** / **DataLake** 블루프린트 enable (VPC·서브넷·S3)
- 프로필 `default-all-capabilities` 생성·ENABLED (인스턴스 `ml.t3.medium,large,xlarge`)
- `CREATE_PROJECT` / `CREATE_PROJECT_FROM_PROJECT_PROFILE` grant (전체 사용자)
- `CREATE_ENVIRONMENT_FROM_BLUEPRINT` grant (project OWNER/CONTRIBUTOR, `accountId:blueprintId`)

### 리소스 이름 규칙 (`agentic-work` 기준)

`role-{purpose}-for-{project}-{region}`, `storage-for-{project}-{account}-{region}`, `lambda-{purpose}-for-{project}`

| 리소스 | 이름 |
|--------|------|
| Domain execution role | `role-domain-execution-for-smus-kr-ap-northeast-1` |
| Domain service role | `role-domain-service-for-smus-kr-ap-northeast-1` |
| Provisioning role | `role-provisioning-for-smus-kr-ap-northeast-1` |
| Manage access role | `role-manage-access-for-smus-kr-ap-northeast-1` |
| Model provisioning / consumption | `role-model-*-for-smus-kr-ap-northeast-1` |
| Project user guardrails 정책 | `policy-project-user-guardrails-for-smus-kr-ap-northeast-1` |
| 프로젝트 S3 | `storage-for-smus-kr-{accountId}-ap-northeast-1` |
| VPC / subnets | `vpc-for-smus-kr`, `subnet-private-{n}-for-smus-kr` |
| Usage monitor Lambda | `lambda-usage-monitor-for-smus-kr` |
| Cost rollup Lambda | `lambda-cost-rollup-for-smus-kr` |
| Inventory / events 테이블 | `smus-kr-resource-inventory`, `smus-kr-usage-events`, `smus-kr-cost-by-user` |
| EventBridge 규칙 | `rule-smus-reconcile-for-smus-kr`, `rule-smus-cost-for-smus-kr`, `rule-smus-api-for-smus-kr`, `rule-smus-notebook-guard-for-smus-kr` |
| CloudWatch Dashboard | `dashboard-smus-kr-usage` |
| Notebooks deny 알람 | `alarm-smus-notebook-deny-for-smus-kr` → SNS `sns-smus-notebook-deny-for-smus-kr` |

## 네트워크: VPC + Bedrock PrivateLink

1. VPC 생성 (DNS hostnames/support ON, 프라이빗 서브넷 ≥ 2 AZ)
2. Interface VPC endpoints (PrivateLink):
   - `com.amazonaws.ap-northeast-1.bedrock-runtime`
   - `com.amazonaws.ap-northeast-1.bedrock-agent`
   - `com.amazonaws.ap-northeast-1.bedrock-agent-runtime`
   - (권장) `sagemaker.api`, `logs`, `monitoring`, `sts`, `s3`(Gateway)
3. Tooling blueprint: `VpcOnly` + 프라이빗 서브넷

## Amazon Bedrock

1. Bedrock 콘솔에서 기반 모델 액세스 부여
2. SMUS 도메인 **Amazon Bedrock models** 탭에서 활성화
3. model provisioning / consumption role 연결
4. 트래픽은 PrivateLink로 VPC 내 유지

## Notebooks vs JupyterLab

SMUS 왼쪽 메뉴에 **Notebooks**와 **JupyterLab**(IDEs)이 따로 있습니다. 둘은 다른 코딩 경험입니다.

| | **Notebooks** (메뉴: Notebooks) | **JupyterLab** (메뉴: IDEs) |
|--|--|--|
| 성격 | SMUS의 **새 서버리스 노트북** UI | 전통적인 **JupyterLab IDE** (Space 기반) |
| 용도 | 데이터 탐색·분석·SQL/Spark에 맞춘 경량 환경 | 커스터마이징·플러그인·본격 ML 개발 |
| 셀 | Python, SQL, Spark, 자연어, 테이블/차트 셀 | 일반 Jupyter 노트북/터미널/파일 브라우저 |
| AI | **SageMaker Data Agent** 내장에 가깝게 설계 | Amazon Q 등 IDE 쪽 보조 |
| 컴퓨트 | 관리형/서버리스 노트북 컴퓨트 (Athena Spark 등) | EC2 Space (`ml.t3.*` 등) |
| 인스턴스 이름 | **`sc.*`** (예: `sc.g5.xlarge`) | **`ml.*`** (예: `ml.t3.large`) |
| 인스턴스 제한 | **사전 IAM Deny 불가** · **자동 Stop도 불가** → **감지·알람만** (아래) | **IAM Deny** (아래) |
| 과금 | 컴퓨트 실행 중 과금, 유휴 시 자동 종료(기본 ~1시간). 서버리스 ≠ 즉시 무과금. 비허용 타입이 뜨면 **수동 Stop 전까지 과금** | Space/`CreateApp` 실행 중 과금 |

한 줄 요약: **Notebooks = 데이터/SQL 중심의 새 노트북**, **JupyterLab = Space를 띄우는 기존 Jupyter IDE**.

### Notebooks (`sc.*`) 제한 — 감지·알람만 (자동 차단 불가)

> **결론**: Notebooks `sc.*`는 JupyterLab처럼 **막을 수 없습니다.**  
> IAM Deny도, Lambda **StopCompute**도, ENI quarantine도 **강제 종료/차단으로 쓸 수 없음**이 확인되어, 운영은 **CloudTrail 감지 → 메트릭/DDB → SNS 알람 → 포털에서 수동 Stop** 으로 합니다.

CloudTrail 기준 Notebooks 기동 API:

| 항목 | 값 |
|------|-----|
| EventSource | `datazone.amazonaws.com` |
| eventName | **`StartNotebookCompute`** |
| 주체 | `datazone_usr_role_*` (프로젝트 사용자 역할) |
| 파라미터 | `computeConfig.instanceType` = `sc.g5.xlarge` 등 |

정책상 허용으로 보는 타입: **`sc.t3.medium`**, **`sc.t3.large`**, **`sc.t3.xlarge`**  
(UI·API에서 그 외 `sc.g5.*` / `sc.g4dn.*` 등도 **시작 가능**합니다.)

#### 왜 자동 차단이 안 되나

| 시도 | 결과 |
|------|------|
| IAM Deny on `datazone:StartNotebookCompute` | 인스턴스 타입 **condition key 없음** → allowlist Deny 불가 |
| Studio `HiddenInstanceTypes` | **`ml.*`만** 대상. **`sc.*` Notebooks UI에는 미적용** |
| Lambda → DataZone `StopCompute` | 포털 Stop에 필요한 `im.amazonaws.com` 세션 컨텍스트가 없어 **403/404** 등 → **강제 Stop 불가** |
| ENI deny-all SG quarantine | 신규 연결에 영향 줄 수 있으나 **이미 떠 있는 커널/실행을 죽이지 못함** |

#### 실제 운영 경로 (감지·알람)

```
CloudTrail StartNotebookCompute (비허용 sc.* 예: sc.g5.xlarge)
        │
        ▼
 EventBridge rule-smus-notebook-guard-for-smus-kr
        │
        ▼
 lambda-usage-monitor (mode=notebook_guard)
        │
        ├─ DynamoDB events/inventory
        ├─ CW metric NotebookComputeDenied / NotebookComputeDeniedTotal
        └─ (Stop/quarantine 시도는 남아 있을 수 있으나 차단 수단으로 신뢰하지 않음)
                │
                ▼
 CloudWatch alarm-smus-notebook-deny-for-smus-kr
        │  NotebookComputeDeniedTotal >= 1 (1분)
        ▼
 SNS sns-smus-notebook-deny-for-smus-kr → email (Confirm 필요)
        │
        ▼
 운영자: Unified Studio 포털에서 해당 Notebook compute 수동 Stop
```

- UI Configure에 `sc.g5.xlarge` 등이 **그대로 노출**됩니다.
- 메트릭: `NotebookComputeAllowed` / `NotebookComputeDenied` / `NotebookComputeDeniedTotal`
- 알람: `alarm-smus-notebook-deny-for-smus-kr` → SNS `sns-smus-notebook-deny-for-smus-kr`  
  (`auth.idc_admin_email` 구독; 메일 **Confirm** 필요)
- `EC2InsufficientCapacityError` 등은 **용량 부족**이지 가드레일 차단이 아님.

참고:

- [Notebooks](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/notebooks.html)
- [Manage compute environments](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/manage-compute-environments.html)
- [JupyterLab IDE](https://docs.aws.amazon.com/sagemaker-unified-studio/latest/userguide/jupyterlab.html)

## 노트북/Space 인스턴스 제한

허용 타입: **`ml.t3.medium`**, **`ml.t3.large`**, **`ml.t3.xlarge`**  
(주로 **JupyterLab Space**의 `CreateApp` / `CreateSpace` / `UpdateSpace`에 적용)

### 동작 방식

| 계층 | 역할 | 효과 |
|------|------|------|
| 프로젝트 프로필 `ec2InstanceTypes` | Tooling 파라미터 | 메타데이터만 (드롭다운 미제한) |
| **IAM Deny 가드레일** | `CreateApp` / `CreateSpace` / `UpdateSpace` 거부 | **하드 제한** (저장·재시작 차단) |
| **Studio UI hide** | `StudioWebPortalSettings.HiddenInstanceTypes` (최대 **20개**) | 흔한 비허용 타입(r5/m5/c5 등)을 드롭다운에서 **숨김** |

- UI hide는 API 한도(20개) 때문에 **전 타입 차단이 아니라 우선 순위 숨김**입니다. 완전한 강제는 IAM Deny입니다.
- Unified Studio JupyterLab **스페이스 구성** UI는 Studio 포털과 다를 수 있어, hide가 항상 반영되지 않을 수 있습니다. 그때도 IAM Deny면 **저장 후 재시작**은 거부됩니다.
- “허용한 것만 보이게” 하는 allowlist UI API는 없고, **숨길 타입을 최대 20개까지 지정**하는 방식만 지원합니다.
- **와일드카드 불가**: `ml.g5.*`, `*.g5.*` 같은 패턴은 지원하지 않습니다. [API enum](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_StudioWebPortalSettings.html)에 있는 **정확한 타입 문자열**만 나열해야 합니다 (예: `ml.g5.xlarge`, `ml.g5.2xlarge` …).
- **Notebooks(`sc.*`)에는 적용되지 않음**: `HiddenInstanceTypes`는 Studio **`ml.*`** UI 드롭다운용입니다. Unified Studio **Notebooks**의 `sc.g5.*` / `sc.g4dn.*` 등은 이 설정으로 **숨길 수 없고**, 시작도 막지 못합니다. Notebooks는 [감지·알람만 (자동 차단 불가)](#notebooks-sc-제한--감지알람만-자동-차단-불가)을 참고하세요.

#### Studio UI hide API (`UpdateDomain`)

인스턴스를 드롭다운에서 최대 20개까지 숨기려면 SageMaker **`UpdateDomain`** 을 사용합니다.  
필드는 `DefaultUserSettings.StudioWebPortalSettings.HiddenInstanceTypes` 입니다.

```bash
aws sagemaker update-domain \
  --domain-id d-rjxdx34xg67o \
  --region ap-northeast-1 \
  --default-user-settings '{
    "StudioWebPortalSettings": {
      "HiddenInstanceTypes": [
        "ml.r5.large",
        "ml.r5.xlarge",
        "ml.m5.large",
        "ml.m5.xlarge"
      ]
    }
  }'
```

```python
sagemaker.update_domain(
    DomainId="d-rjxdx34xg67o",
    DefaultUserSettings={
        "StudioWebPortalSettings": {
            "HiddenInstanceTypes": ["ml.r5.large", "..."]  # max 20; exact enum only — no wildcards
        }
    },
)
```

g5 계열을 숨기려면 와일드카드 없이 **개별 나열**해야 합니다 (8개가 20개 슬롯을 차지함):

```json
"HiddenInstanceTypes": [
  "ml.g5.xlarge", "ml.g5.2xlarge", "ml.g5.4xlarge", "ml.g5.8xlarge",
  "ml.g5.12xlarge", "ml.g5.16xlarge", "ml.g5.24xlarge", "ml.g5.48xlarge"
]
```

> **주의**: 위 예시는 **Space/Studio `ml.g5.*` UI hide**입니다. **Notebooks `sc.g5.*`는 대상이 아닙니다.**

`installer.py`의 `apply_studio_ui_instance_hide()`가 설치 시 이 API를 호출합니다 (허용 3종 제외, 흔한 비허용 타입 우선 최대 20개).

### 자동 적용

정책: `policy-project-user-guardrails-for-smus-kr-ap-northeast-1`

- Actions: `sagemaker:CreateApp`, `sagemaker:CreateSpace`, `sagemaker:UpdateSpace`
- Condition: `sagemaker:InstanceTypes` ∉ `{ml.t3.medium, ml.t3.large, ml.t3.xlarge}`

#### IAM Deny 정책 + 역할 attach 예제

허용 타입만 쓰게 하려면 프로젝트 사용자 역할(`datazone_usr_role_*`)에 아래 Deny 정책을 붙입니다.  
역할에 “허용 목록을 Allow로 쓰는” 방식이 아니라, **비허용 타입을 explicit Deny** 하는 방식입니다.

정책 문서 예제:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnauthorizedInstanceTypes",
      "Effect": "Deny",
      "Action": [
        "sagemaker:CreateApp",
        "sagemaker:CreateSpace",
        "sagemaker:UpdateSpace"
      ],
      "Resource": "*",
      "Condition": {
        "ForAnyValue:StringNotEquals": {
          "sagemaker:InstanceTypes": [
            "ml.t3.medium",
            "ml.t3.large",
            "ml.t3.xlarge"
          ]
        }
      }
    }
  ]
}
```

정책 생성 후 역할에 연결:

```bash
# 정책 생성 (최초 1회)
aws iam create-policy \
  --policy-name policy-project-user-guardrails-for-smus-kr-ap-northeast-1 \
  --policy-document file://guardrails-instance-deny.json

# 프로젝트 사용자 역할에 attach
aws iam attach-role-policy \
  --role-name datazone_usr_role_algrwbi69cawex_40meqympqa5q1l \
  --policy-arn arn:aws:iam::262976740991:policy/policy-project-user-guardrails-for-smus-kr-ap-northeast-1
```

```python
iam.create_policy(
    PolicyName="policy-project-user-guardrails-for-smus-kr-ap-northeast-1",
    PolicyDocument=json.dumps(guardrails_doc),
)
iam.attach_role_policy(
    RoleName="datazone_usr_role_algrwbi69cawex_40meqympqa5q1l",
    PolicyArn="arn:aws:iam::262976740991:policy/policy-project-user-guardrails-for-smus-kr-ap-northeast-1",
)
```

`installer.py`는 위 정책을 생성한 뒤 `attach_guardrails_to_project_roles()`로 `datazone_usr_role_*`에 자동 attach 합니다.

#### 프로젝트 역할 자동 attach 구현

SMUS가 프로젝트를 만들 때 IAM 역할 `datazone_usr_role_<projectId>_<envId>` 를 생성합니다.  
이 역할에 Deny 정책을 붙이지 않으면 JupyterLab에서 비허용 인스턴스로 바꿀 수 있습니다.

**1) 설치 시 (`installer.py` → `attach_guardrails_to_project_roles`)**

1. 계정 내 IAM 역할을 `list_roles`로 순회
2. 이름이 `datazone_usr_role_` 로 시작하는 역할만 선택
3. Guardrails 정책(인스턴스 Deny + Data Agent Deny) `AttachRolePolicy`
4. (레거시) Notebooks Stop 시도용 inline/`trust`가 남아 있을 수 있으나 **자동 차단 수단으로 쓰지 않음** — 운영은 감지·알람
5. 결과를 `config.json` → `resources.guardrails_attached_roles` 에 기록

도메인/프로필 설정 직후 `main()`에서 한 번 호출됩니다.  
같은 실행에서 `apply_studio_ui_instance_hide`로 SageMaker 도메인 `HiddenInstanceTypes`(최대 20개)도 적용합니다.

Guardrails 정책에는 JupyterLab Deny와 함께 Data Agent Deny가 포함됩니다:  
`datazone:SendMessage|GenerateCode|StartConversation|GetConversation|ListConversations`

**2) 이후 신규 프로젝트 (`lambda-usage-monitor` reconcile)**

프로젝트는 installer 이후에 만들어지므로, 새 `datazone_usr_role_*`는 설치 시점 scan에 없습니다.

- EventBridge `rate(15 minutes)` → `lambda-usage-monitor-for-smus-kr` (`mode=reconcile`)
- reconcile 끝에서 `attach_guardrails_to_project_roles()` 재실행 (Deny attach + notebook-stop inline/trust)
- 정책 ARN은 Lambda 환경변수 `GUARDRAILS_POLICY_ARN`, allowlist는 `ALLOWED_NOTEBOOK_INSTANCE_TYPES`
- Lambda 실행 역할: `iam:ListRoles` / `ListAttachedRolePolicies` / `AttachRolePolicy` / `PutRolePolicy` / `UpdateAssumeRolePolicy` / `sts:AssumeRole` on `datazone_usr_role_*`

```
installer.py
  └─ attach_guardrails_to_project_roles()
        ├─ Deny(guardrails) attach
        ├─ inline smus-notebook-compute-stop
        └─ trust usage-monitor Lambda

프로젝트 신규 생성 (SMUS)
  └─ datazone_usr_role_NEW 생성 (정책 없음)
        │
        ▼ (최대 ~15분)
lambda-usage-monitor reconcile
  └─ 동일 attach (Deny + notebook-stop inline/trust)
```

### 기본값

도메인/Space에 `DefaultResourceSpec`을 지정하지 않으면 JupyterLab 기본 인스턴스는 보통 **`ml.t3.medium`**(2 vCPU / 4 GiB)입니다.

> `ml.t3.medium`은 메모리가 작아 Space/원격 IDE에서 부족할 수 있습니다. 실사용은 **`ml.t3.large`**(8 GiB) 권장.

## SageMaker Data Agent 비활성화

1. **IdC 도메인 토글 OFF** → UI에서 Data Agent 진입점 비활성/미제공
2. **IAM Deny** (우회 방지): `datazone:SendMessage|GenerateCode|StartConversation|GetConversation|ListConversations`

## 모니터링 아키텍처 (중요)

IdC 사용자가 만든 **노트북/Space 종류·상태·사용 형태**와 **사용자별 비용**을 보기 위해 아래를 구성합니다.

```
CloudTrail / SageMaker API 이벤트
        │
        ├─ Space/App API ──► rule-smus-api-for-smus-kr
        └─ StartNotebookCompute ──► rule-smus-notebook-guard-for-smus-kr
                │
                ▼
         lambda-usage-monitor ──► DynamoDB (events + inventory)
                ▲                      │
                │   schedule (15m)     ├─ Notebooks: Denied 메트릭
                └─ reconcile (+ guardrails attach)
                                          │
 Cost Explorer (일 1회)                    ├─ alarm → SNS (수동 Stop 유도)
        │                                 ▼
 lambda-cost-rollup → cost-by-user     CloudWatch Metrics / Dashboard
```

### 무엇을 보나

| 관점 | 소스 | 대시보드/테이블 |
|------|------|----------------|
| 노트북/Space **종류** (JupyterLab, Code Editor, instance type) | `ListSpaces` / `DescribeSpace` / CreateApp 이벤트 | inventory + CW widget |
| **사용 형태** (InService / Pending / Failed / 삭제, idle) | 주기 reconcile + 이벤트 | inventory status, last_seen |
| 사용자 (IdC user profile) | `sagemaker:user-profile-arn` / OwnerUserProfileName | per-user breakdown |
| **사용자별 비용** | Cost Explorer + cost allocation tags (`sagemaker:user-profile-arn`, `sagemaker:space-arn`, `Project=smus-kr`) | `smus-kr-cost-by-user` |

### 제안 (installer에 포함 / 권장)

| 우선순위 | 항목 | 이유 |
|----------|------|------|
| P0 | Space/App **인벤토리 reconcile** Lambda | 실시간 이벤트만으로는 누락·드리프트 발생 |
| P0 | EventBridge → usage Lambda | 생성/변경/삭제·인스턴스 타입 변경 추적 |
| P0 | CloudWatch Dashboard | 운영자가 한 화면에서 현황 확인 |
| P1 | Cost allocation tag 활성화 + **일별 Cost Explorer rollup** | 사용자별 비용 (태그 활성화 후 24~48시간 지연) |
| P1 | `sourceIdentity` / user-profile 태그 강제 | CloudTrail·비용 귀속 정확도 |
| P2 | Idle Space 알림 (N일 InService + 무활동) | 비용 누수 방지 |
| P0 | Notebooks deny SNS 알람 (`NotebookComputeDeniedTotal`) | sc.* 강제 Stop 불가 → 감지 즉시 알림 |
| P2 | Budgets + SNS (사용자/도메인 한도) | 예산 초과 조기 경보 |
| P3 | Athena + CUR 상세 분석 | 장기·세분화 비용 리포트 |

> 사용자별 비용은 **가능합니다.** 다만 Cost Explorer/CUR에서 `sagemaker:user-profile-arn`(또는 space ARN)을 **비용 할당 태그로 활성화**해야 하며, 활성화 전 비용은 소급되지 않습니다. Shared space는 사용자 단위 귀속이 어렵습니다.
