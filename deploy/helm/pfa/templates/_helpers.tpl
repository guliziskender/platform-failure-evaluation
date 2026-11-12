{{- define "pfa.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "pfa.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "pfa.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "pfa.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pfa.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- required "serviceAccount.name is required when serviceAccount.create is false" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "pfa.validate" -}}
{{- $provider := .Values.ai.provider -}}
{{- if not (has $provider (list "none" "anthropic" "ollama")) -}}
{{- fail (printf "ai.provider must be none, anthropic or ollama, not %q" $provider) -}}
{{- end -}}
{{- if and (eq $provider "anthropic") (not .Values.ai.existingSecret) -}}
{{- fail "ai.provider=anthropic needs ai.existingSecret: a Secret you create holding the API key" -}}
{{- end -}}
{{- if not (has .Values.rbac.scope (list "cluster" "namespaces")) -}}
{{- fail (printf "rbac.scope must be cluster or namespaces, not %q" .Values.rbac.scope) -}}
{{- end -}}
{{- if and (eq .Values.rbac.scope "namespaces") (not .Values.rbac.namespaces) -}}
{{- fail "rbac.scope=namespaces needs rbac.namespaces: the namespaces pfa may read" -}}
{{- end -}}
{{- if and .Values.networkPolicy.enabled (not .Values.networkPolicy.apiServer.cidrs) -}}
{{- fail "networkPolicy.enabled needs networkPolicy.apiServer.cidrs, the API server's real addresses: kubectl get endpointslice -n default -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[0].endpoints[*].addresses[*]}'" -}}
{{- end -}}
{{- end -}}

{{/* The pod spec shared by the toolbox Deployment and the diagnose Job. */}}
{{- define "pfa.podSpec" -}}
serviceAccountName: {{ include "pfa.serviceAccountName" . }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
containers:
  - name: pfa
    image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    {{- with .args }}
    args:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .command }}
    command:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    env:
      - name: PFA_CONFIG
        value: /etc/pfa/config.yaml
      {{- if .Values.ai.existingSecret }}
      - name: ANTHROPIC_API_KEY
        valueFrom:
          secretKeyRef:
            name: {{ .Values.ai.existingSecret }}
            key: {{ .Values.ai.existingSecretKey }}
      {{- end }}
    securityContext:
      {{- toYaml .Values.securityContext | nindent 6 }}
    resources:
      {{- toYaml .Values.resources | nindent 6 }}
    volumeMounts:
      - {name: config, mountPath: /etc/pfa, readOnly: true}
      - {name: tmp, mountPath: /tmp}
volumes:
  - name: config
    configMap:
      name: {{ include "pfa.fullname" . }}
  - name: tmp
    emptyDir: {sizeLimit: 64Mi}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
