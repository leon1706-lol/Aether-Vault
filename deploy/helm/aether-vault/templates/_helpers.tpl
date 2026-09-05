{{/*
Chart name and fullname helpers — standard Helm chart-starter pattern.
*/}}
{{- define "aether-vault.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "aether-vault.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "aether-vault.labels" -}}
app.kubernetes.io/name: {{ include "aether-vault.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "aether-vault.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aether-vault.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the Secret holding DATABASE_URL/AV_APP_DATABASE_URL/REDIS_URL/AV_API_TOKEN/
AV_AUTH_USERS — either the chart-managed one (templates/secret.yaml) or a caller-supplied
existingSecret, so a real deployment never needs to put credentials in `helm --set` /
values files at all.
*/}}
{{- define "aether-vault.secretName" -}}
{{- if .Values.database.existingSecret }}
{{- .Values.database.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "aether-vault.fullname" .) }}
{{- end }}
{{- end }}
