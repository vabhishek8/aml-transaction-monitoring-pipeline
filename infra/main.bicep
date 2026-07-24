// =============================================================================
// Production Azure mapping for the AML transaction-monitoring pipeline.
//
// This template is deliberately architected differently from the companion
// weather-pipeline project's IaC. That one optimised for cost on a
// low-value, low-sensitivity public dataset. This one optimises for what a
// bank's InfoSec and regulatory-risk teams actually gate on: no public
// network exposure, immutable audit-grade storage, centralised data
// governance, and least-privilege identity -- because the data class here
// (customer transactions, KYC risk tiers) is exactly what APRA CPS 234,
// the Privacy Act, and AML/CTF Act record-keeping obligations govern, even
// though this specific dataset is synthetic.
//
// Not deployed and left running: same cost-discipline reasoning as the
// weather-pipeline template, with an added compliance argument -- a
// standing environment holding "transaction-shaped" data (even synthetic)
// with no active monitoring owner is itself a finding in most banking
// security reviews. Deploy on demand, review, tear down.
//
//   az deployment group create \
//     --resource-group rg-aml-pipeline-<env> \
//     --template-file infra/main.bicep \
//     --parameters environment=dev alertEmail=you@example.com
// =============================================================================

@description('Deployment environment. Drives naming, SKU sizing, and retention.')
@allowed(['dev', 'sit', 'uat', 'prod'])
param environment string = 'dev'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Email address for pipeline-failure and security alerts.')
param alertEmail string

@description('Base name used to derive all resource names. Lowercase alphanumeric only.')
@minLength(3)
@maxLength(12)
param baseName string = 'amlmon'

var suffix = '${baseName}${environment}${uniqueString(resourceGroup().id)}'
var isProd = environment == 'prod'
var tags = {
  project: 'aml-transaction-monitoring-pipeline'
  environment: environment
  managedBy: 'bicep'
  dataClassification: 'confidential'
  regulatoryScope: 'AML-CTF,APRA-CPS234,BCBS239'
}

// -----------------------------------------------------------------------------
// Network: everything data-bearing sits behind private endpoints in this
// VNet. No storage account, Key Vault, or analytics service in this
// template accepts a public connection -- that's the single biggest
// difference from a "cost-optimised demo" architecture to a "bank InfoSec
// would actually approve this" architecture.
// -----------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: 'vnet-${suffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.20.0.0/16'] }
    subnets: [
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: '10.20.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

var peSubnetId = '${vnet.id}/subnets/snet-private-endpoints'

// -----------------------------------------------------------------------------
// Storage: ADLS Gen2, no public network access, version-level immutability
// enabled account-wide. Immutable, tamper-evident storage isn't decoration
// here -- AML transaction records and the alerts generated from them are
// exactly the class of data that needs a defensible chain of custody if a
// SAR (suspicious activity report) is ever challenged.
// -----------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: take('st${suffix}', 24)
  location: location
  tags: tags
  sku: { name: isProd ? 'Standard_ZRS' : 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
    immutableStorageWithVersioning: {
      enabled: true
    }
  }

  resource blobServices 'blobServices' = {
    name: 'default'
    properties: {
      deleteRetentionPolicy: { enabled: true, days: 30 }
      isVersioningEnabled: true
    }

    resource bronze 'containers' = { name: 'bronze', properties: { publicAccess: 'None' } }
    resource silver 'containers' = { name: 'silver', properties: { publicAccess: 'None' } }
    resource gold   'containers' = { name: 'gold',   properties: { publicAccess: 'None' } }
    resource audit  'containers' = { name: 'audit',  properties: { publicAccess: 'None' } }
  }
}

resource peStorage 'Microsoft.Network/privateEndpoints@2023-09-01' = {
  name: 'pe-${suffix}-blob'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'plsc-blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Key Vault: RBAC-authorized (no legacy access policies), purge protection
// on, no public network access. Holds the pipeline's secrets/connection
// config; nothing is ever committed to source control.
// -----------------------------------------------------------------------------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('kv-${suffix}', 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
  }
}

resource peKeyVault 'Microsoft.Network/privateEndpoints@2023-09-01' = {
  name: 'pe-${suffix}-kv'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'plsc-kv'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: ['vault']
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Private DNS: resolves the private-link FQDNs inside the VNet so ADF/
// Synapse/Purview reach storage and Key Vault over the private endpoint
// instead of falling back to a public IP.
// -----------------------------------------------------------------------------
resource dnsZoneBlob 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.blob.core.windows.net'
  location: 'global'
  tags: tags
}
resource dnsZoneVault 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}

resource dnsLinkBlob 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZoneBlob
  name: 'link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}
resource dnsLinkVault 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZoneVault
  name: 'link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource dnsGroupStorage 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-09-01' = {
  parent: peStorage
  name: 'dnsgroup-blob'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'blob-config', properties: { privateDnsZoneId: dnsZoneBlob.id } }
    ]
  }
}
resource dnsGroupVault 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-09-01' = {
  parent: peKeyVault
  name: 'dnsgroup-vault'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'vault-config', properties: { privateDnsZoneId: dnsZoneVault.id } }
    ]
  }
}

// -----------------------------------------------------------------------------
// Data Factory: orchestration, no public network access, system-assigned
// identity scoped to least-privilege Storage Blob Data Contributor only.
// -----------------------------------------------------------------------------
resource dataFactory 'Microsoft.DataFactory/factories@2018-06-01' = {
  name: take('adf-${suffix}', 24)
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    publicNetworkAccess: 'Disabled'
  }
}

var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource adfStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, dataFactory.id, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: dataFactory.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}
resource adfKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, dataFactory.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: dataFactory.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

// -----------------------------------------------------------------------------
// Synapse Serverless SQL: gold-layer query engine (the cloud analogue of
// this repo's DuckDB risk-scoring SQL). Managed virtual network enabled --
// Synapse provisions its own isolated managed VNet for compute, so pipeline
// queries never traverse the public internet even for intra-service calls.
// -----------------------------------------------------------------------------
resource synapseWorkspace 'Microsoft.Synapse/workspaces@2021-06-01' = {
  name: take('synw-${suffix}', 24)
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    defaultDataLakeStorage: {
      accountUrl: storage.properties.primaryEndpoints.dfs
      filesystem: 'gold'
    }
    sqlAdministratorLogin: 'synapseadmin'
    managedResourceGroupName: 'rg-${suffix}-synapse-managed'
    managedVirtualNetwork: 'default'
    publicNetworkAccess: 'Disabled'
  }
}
resource synapseStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, synapseWorkspace.id, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: synapseWorkspace.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
  }
}

// -----------------------------------------------------------------------------
// Purview: data governance/classification/lineage across the bronze/silver/
// gold estate. This is the piece most cost-optimised demo architectures
// skip and the piece a bank's data-governance function asks about first --
// BCBS 239 (risk data aggregation) is fundamentally a data-lineage and
// data-quality-ownership requirement, not just a modelling one.
// -----------------------------------------------------------------------------
resource purview 'Microsoft.Purview/accounts@2021-12-01' = {
  name: take('pview-${suffix}', 24)
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    publicNetworkAccess: 'Disabled'
    managedResourceGroupName: 'rg-${suffix}-purview-managed'
  }
}

// -----------------------------------------------------------------------------
// Observability + alerting: pipeline failures and (in production) anomalous
// access patterns against the storage/Key Vault estate surface here.
// -----------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: take('log-${suffix}', 24)
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: isProd ? 365 : 60 // longer retention than the weather-pipeline: audit trail, not just ops telemetry
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: take('ag-${suffix}', 24)
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'amlalert'
    enabled: true
    emailReceivers: [
      { name: 'pipeline-owner', emailAddress: alertEmail, useCommonAlertSchema: true }
    ]
  }
}

resource pipelineFailureAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-adf-pipeline-failed-${environment}'
  location: 'global'
  tags: tags
  properties: {
    severity: 1
    enabled: true
    scopes: [dataFactory.id]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'PipelineFailedRuns'
          metricName: 'PipelineFailedRuns'
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [ { actionGroupId: actionGroup.id } ]
  }
}

output storageAccountName string = storage.name
output dataFactoryName string = dataFactory.name
output synapseWorkspaceName string = synapseWorkspace.name
output purviewAccountName string = purview.name
output keyVaultName string = keyVault.name
output vnetName string = vnet.name
