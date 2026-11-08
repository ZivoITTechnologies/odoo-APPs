"""Shopify Admin GraphQL documents, validated against the official 2026-10 schema.

Shopify rejects any single query whose requested cost exceeds 1000 points, so large
objects are read in several small documents and assembled in Python. The test-suite
measures every document here against that limit.
"""

PAGE_INFO = "pageInfo { hasNextPage endCursor }"

SHOP = """
query ZivoShop {
  shop {
    id name email myshopifyDomain currencyCode ianaTimezone taxesIncluded weightUnit
    enabledPresentmentCurrencies
    primaryDomain { url host }
    plan { publicDisplayName shopifyPlus }
  }
}"""

LOCATIONS = """
query ZivoLocations($cursor: String) {
  locations(first: 100, after: $cursor, includeLegacy: true, includeInactive: false) {
    nodes { id name isActive fulfillsOnlineOrders isFulfillmentService
      address { address1 city zip province country countryCode } }
    %s
  }
}""" % PAGE_INFO

PUBLICATIONS = """
query ZivoPublications($cursor: String) {
  publications(first: 50, after: $cursor) {
    nodes { id catalog { title } }
    %s
  }
}""" % PAGE_INFO

VARIANT_FIELDS = """
fragment ZivoVariant on ProductVariant {
  id title sku price compareAtPrice position taxable inventoryPolicy
  barcodes(first: 1) { nodes { value } }
  selectedOptions { name value }
  inventoryItem { id tracked unitCost { amount } }
}"""

PRODUCT_FIELDS = """
fragment ZivoProduct on Product {
  id title handle status vendor productType tags descriptionHtml updatedAt
  seo { title description }
  options { id name position values optionValues { id name } }
  media(first: 10) { nodes { ... on MediaImage { id alt image { url altText } } } }
  metafields(first: 25) { nodes { namespace key type value } }
  collections(first: 10) { nodes { id title handle sources { id __typename } } }
  variants(first: 50) { nodes { ...ZivoVariant } %s }
}""" % PAGE_INFO

PRODUCTS = """
query ZivoProducts($query: String, $cursor: String) {
  products(first: 100, after: $cursor, query: $query, sortKey: UPDATED_AT) {
    nodes { id title }
    %s
  }
}""" % PAGE_INFO

PRODUCT = """
query ZivoProduct($id: ID!) {
  product(id: $id) { ...ZivoProduct }
}""" + PRODUCT_FIELDS + VARIANT_FIELDS

PRODUCT_VARIANTS_PAGE = """
query ZivoProductVariants($id: ID!, $cursor: String) {
  product(id: $id) { variants(first: 100, after: $cursor) { nodes { ...ZivoVariant } %s } }
}""" % PAGE_INFO + VARIANT_FIELDS

PRODUCT_SET = """
mutation ZivoProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(input: $input, identifier: $identifier, synchronous: true) {
    product { ...ZivoProduct }
    userErrors { field message code }
  }
}""" + PRODUCT_FIELDS + VARIANT_FIELDS

PRODUCT_STATUS = """
mutation ZivoProductStatus($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id status }
    userErrors { field message }
  }
}"""

VARIANTS_BULK_UPDATE = """
mutation ZivoVariantPrices($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants, allowPartialUpdates: true) {
    productVariants { id price compareAtPrice }
    userErrors { field message code }
  }
}"""

PUBLISH = """
mutation ZivoPublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) { userErrors { field message } }
}"""

UNPUBLISH = """
mutation ZivoUnpublish($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) { userErrors { field message } }
}"""

METAFIELDS_SET = """
mutation ZivoMetafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key }
    userErrors { field message code }
  }
}"""

INVENTORY_SET = """
mutation ZivoInventorySet($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    inventoryAdjustmentGroup { id }
    userErrors { field message code }
  }
}"""

INVENTORY_ACTIVATE = """
mutation ZivoInventoryActivate($inventoryItemId: ID!, $locationId: ID!, $key: String!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) @idempotent(key: $key) {
    inventoryLevel { id }
    userErrors { field message }
  }
}"""

INVENTORY_LEVELS = """
query ZivoInventoryLevels($location: ID!, $cursor: String) {
  location(id: $location) {
    inventoryLevels(first: 250, after: $cursor) {
      nodes { item { id } quantities(names: ["available", "on_hand"]) { name quantity } }
      %s
    }
  }
}""" % PAGE_INFO

ADDRESS_FIELDS = """
fragment ZivoAddress on MailingAddress {
  firstName lastName name company address1 address2 city province provinceCode zip
  country countryCodeV2 phone
}"""

MONEY_BAG = "{ shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }"

CUSTOMER_FIELDS = """
fragment ZivoCustomer on Customer {
  id firstName lastName displayName locale note tags taxExempt numberOfOrders updatedAt
  defaultEmailAddress { emailAddress }
  defaultPhoneNumber { phoneNumber }
  defaultAddress { ...ZivoAddress }
  amountSpent { amount currencyCode }
  metafields(first: 20) { nodes { namespace key type value } }
}"""

ORDER_FIELDS = """
fragment ZivoOrder on Order {
  id name createdAt updatedAt processedAt cancelledAt cancelReason closedAt test edited note tags
  email phone sourceName taxesIncluded currencyCode presentmentCurrencyCode paymentGatewayNames
  displayFinancialStatus displayFulfillmentStatus discountCodes poNumber returnStatus
  billingAddressMatchesShippingAddress
  retailLocation { id name }
  totalPriceSet %(money)s
  currentTotalPriceSet %(money)s
  totalTaxSet %(money)s
  totalTipReceivedSet %(money)s
  totalRefundedSet %(money)s
  customer { ...ZivoCustomer }
  purchasingEntity { __typename ... on PurchasingCompany { company { id name } } }
  billingAddress { ...ZivoAddress }
  shippingAddress { ...ZivoAddress }
  risk { recommendation assessments { riskLevel facts { description sentiment } } }
  metafields(first: 20) { nodes { namespace key type value } }
  shippingLines(first: 5) { nodes {
    title code isRemoved
    originalPriceSet %(money)s
    discountedPriceSet %(money)s
    taxLines { title rate priceSet %(money)s }
  } }
  transactions(first: 30) {
    id kind status gateway processedAt test
    amountSet %(money)s
    parentTransaction { id }
    fees { amount { amount currencyCode } }
  }
  fulfillments(first: 50) { id }
  refunds(first: 50) { id }
}""" % {"money": MONEY_BAG}

ORDERS = """
query ZivoOrders($query: String, $cursor: String) {
  orders(first: 100, after: $cursor, query: $query, sortKey: UPDATED_AT) {
    nodes { id name }
    %s
  }
}""" % PAGE_INFO

ORDER = """
query ZivoOrderOne($id: ID!) {
  order(id: $id) { ...ZivoOrder }
}""" + ORDER_FIELDS + CUSTOMER_FIELDS + ADDRESS_FIELDS

ORDER_LINES = """
query ZivoOrderLines($id: ID!, $cursor: String) {
  order(id: $id) {
    lineItems(first: 50, after: $cursor) {
      nodes {
        id name title sku quantity currentQuantity requiresShipping taxable
        originalUnitPriceSet %(money)s
        totalDiscountSet %(money)s
        taxLines { title rate priceSet %(money)s }
        variant { id }
      }
      %(page)s
    }
  }
}""" % {"money": MONEY_BAG, "page": PAGE_INFO}

FULFILLMENT_NODE = """
query ZivoFulfillmentNode($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Fulfillment {
      id name status createdAt
      location { id }
      trackingInfo(first: 5) { company number url }
      fulfillmentLineItems(first: 100, after: $cursor) {
        nodes { id quantity lineItem { id } }
        %s
      }
    }
  }
}""" % PAGE_INFO

REFUND_NODE = """
query ZivoRefundNode($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Refund {
      id note createdAt
      totalRefundedSet %(money)s
      refundLineItems(first: 50, after: $cursor) {
        nodes {
          quantity restockType restocked
          lineItem { id }
          location { id }
          subtotalSet %(money)s
          totalTaxSet %(money)s
        }
        %(page)s
      }
      refundShippingLines(first: 5) { nodes {
        subtotalAmountSet %(money)s
        taxAmountSet %(money)s
      } }
      transactions(first: 10) { nodes {
        id kind status gateway
        amountSet %(money)s
      } }
    }
  }
}""" % {"money": MONEY_BAG, "page": PAGE_INFO}

ORDER_RETURNS = """
query ZivoOrderReturns($id: ID!, $cursor: String) {
  order(id: $id) {
    returns(first: 5, after: $cursor) {
      nodes {
        id name status
        returnLineItems(first: 20) { nodes {
          ... on ReturnLineItem {
            id quantity customerNote
            returnReasonDefinition { name }
            fulfillmentLineItem { lineItem { id } }
          }
        } }
      }
      %s
    }
  }
}""" % PAGE_INFO

VARIANT_PRODUCT = """
query ZivoVariantProduct($id: ID!) {
  productVariant(id: $id) { id product { id } }
}"""

ORDER_CANCEL = """
mutation ZivoOrderCancel($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!,
                         $notifyCustomer: Boolean, $staffNote: String,
                         $refundMethod: OrderCancelRefundMethodInput) {
  orderCancel(orderId: $orderId, reason: $reason, restock: $restock, notifyCustomer: $notifyCustomer,
              staffNote: $staffNote, refundMethod: $refundMethod) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}"""

ORDER_CREATE = """
mutation ZivoOrderCreate($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order { id name }
    userErrors { field message code }
  }
}"""

ORDER_EDIT_BEGIN = """
mutation ZivoOrderEditBegin($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder { id lineItems(first: 250) { nodes { id quantity variant { id } } } }
    userErrors { field message }
  }
}"""

ORDER_EDIT_SET_QTY = """
mutation ZivoOrderEditSetQty($id: ID!, $lineItemId: ID!, $quantity: Int!) {
  orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity, restock: true) {
    calculatedOrder { id }
    userErrors { field message }
  }
}"""

ORDER_EDIT_ADD_VARIANT = """
mutation ZivoOrderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity, allowDuplicates: true) {
    calculatedOrder { id }
    userErrors { field message }
  }
}"""

ORDER_EDIT_COMMIT = """
mutation ZivoOrderEditCommit($id: ID!, $notifyCustomer: Boolean, $staffNote: String) {
  orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    order { id }
    userErrors { field message }
  }
}"""

FULFILLMENT_ORDERS = """
query ZivoFulfillmentOrders($id: ID!) {
  order(id: $id) {
    id
    fulfillmentOrders(first: 10) { nodes {
      id status supportedActions { action }
      assignedLocation { location { id } }
      lineItems(first: 40) { nodes { id remainingQuantity totalQuantity lineItem { id } } %s }
    } }
  }
}""" % PAGE_INFO

FULFILLMENT_ORDER_LINES = """
query ZivoFulfillmentOrderLines($id: ID!, $cursor: String) {
  fulfillmentOrder(id: $id) {
    lineItems(first: 100, after: $cursor) {
      nodes { id remainingQuantity totalQuantity lineItem { id } }
      %s
    }
  }
}""" % PAGE_INFO

FULFILLMENT_CREATE = """
mutation ZivoFulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status }
    userErrors { field message }
  }
}"""

SUGGESTED_REFUND = """
query ZivoSuggestedRefund($id: ID!, $lines: [RefundLineItemInput!], $shipping: Money) {
  order(id: $id) {
    suggestedRefund(refundLineItems: $lines, shippingAmount: $shipping) {
      suggestedTransactions {
        gateway kind
        amountSet %s
        parentTransaction { id }
      }
    }
  }
}""" % MONEY_BAG

REFUND_CREATE = """
mutation ZivoRefundCreate($input: RefundInput!, $key: String!) {
  refundCreate(input: $input) @idempotent(key: $key) {
    refund { id }
    userErrors { field message }
  }
}"""

RETURN_CREATE = """
mutation ZivoReturnCreate($input: ReturnInput!) {
  returnCreate(returnInput: $input) {
    return { id name status }
    userErrors { field message code }
  }
}"""

CUSTOMERS = """
query ZivoCustomers($query: String, $cursor: String) {
  customers(first: 25, after: $cursor, query: $query, sortKey: UPDATED_AT) {
    nodes { ...ZivoCustomer }
    %s
  }
}""" % PAGE_INFO + CUSTOMER_FIELDS + ADDRESS_FIELDS

CUSTOMER = """
query ZivoCustomerOne($id: ID!) {
  customer(id: $id) { ...ZivoCustomer }
}""" + CUSTOMER_FIELDS + ADDRESS_FIELDS

CUSTOMER_SET = """
mutation ZivoCustomerSet($input: CustomerSetInput!, $identifier: CustomerSetIdentifiers) {
  customerSet(input: $input, identifier: $identifier) {
    customer { id }
    userErrors { field message code }
  }
}"""

SEGMENTS = """
query ZivoSegments($cursor: String) {
  segments(first: 50, after: $cursor) {
    nodes { id name query }
    %s
  }
}""" % PAGE_INFO

SEGMENT_MEMBERS = """
query ZivoSegmentMembers($segment: ID!, $cursor: String) {
  customerSegmentMembers(first: 250, after: $cursor, segmentId: $segment) {
    edges { node { id defaultEmailAddress { emailAddress } } }
    %s
  }
}""" % PAGE_INFO

ABANDONED_CHECKOUTS = """
query ZivoCheckouts($query: String, $cursor: String) {
  abandonedCheckouts(first: 8, after: $cursor, query: $query) {
    nodes {
      id name abandonedCheckoutUrl createdAt updatedAt completedAt note
      totalPriceSet %(money)s
      customer { id firstName lastName defaultEmailAddress { emailAddress } defaultPhoneNumber { phoneNumber } }
      billingAddress { ...ZivoAddress }
      lineItems(first: 20) { nodes { id title variantTitle quantity sku variant { id }
        discountedTotalPriceSet %(money)s } }
    }
    %(page)s
  }
}""" % {"money": MONEY_BAG, "page": PAGE_INFO} + ADDRESS_FIELDS

PAYOUTS = """
query ZivoPayouts($cursor: String) {
  shopifyPaymentsAccount {
    payouts(first: 50, after: $cursor, reverse: true) {
      nodes {
        id status issuedAt transactionType
        net { amount currencyCode }
        summary {
          chargesGross { amount } chargesFee { amount }
          refundsFeeGross { amount } refundsFee { amount }
          adjustmentsGross { amount } adjustmentsFee { amount }
          reservedFundsGross { amount } reservedFundsFee { amount }
          retriedPayoutsGross { amount } retriedPayoutsFee { amount }
        }
      }
      %s
    }
  }
}""" % PAGE_INFO

BALANCE_TRANSACTIONS = """
query ZivoBalanceTransactions($cursor: String) {
  shopifyPaymentsAccount {
    balanceTransactions(first: 100, after: $cursor, reverse: true) {
      nodes {
        id type sourceType sourceOrderTransactionId transactionDate test
        amount { amount currencyCode }
        fee { amount }
        net { amount }
        associatedOrder { id name }
        associatedPayout { id status }
      }
      %s
    }
  }
}""" % PAGE_INFO

MARKETS = """
query ZivoMarkets($cursor: String) {
  markets(first: 5, after: $cursor) {
    nodes {
      id name handle status type
      conditions { regionsCondition { regions(first: 120) { nodes { ... on MarketRegionCountry { code name } } } } }
      catalogs(first: 3) { nodes { id title status priceList { id name currency } } }
    }
    %s
  }
}""" % PAGE_INFO

PRICE_LIST_ADD = """
mutation ZivoPriceListAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
  priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
    prices { price { amount } }
    userErrors { field message code }
  }
}"""

WEBHOOKS = """
query ZivoWebhooks($cursor: String) {
  webhookSubscriptions(first: 100, after: $cursor) {
    nodes { id topic uri }
    %s
  }
}""" % PAGE_INFO

WEBHOOK_CREATE = """
mutation ZivoWebhookCreate($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}"""

WEBHOOK_DELETE = """
mutation ZivoWebhookDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}"""

SHOP_LOCALES = """
query ZivoShopLocales {
  shopLocales { locale name primary published }
}"""

TRANSLATABLE_RESOURCES = """
query ZivoTranslatable($ids: [ID!]!, $locale: String!, $cursor: String) {
  translatableResourcesByIds(first: 50, after: $cursor, resourceIds: $ids) {
    nodes {
      resourceId
      translatableContent { key value digest locale }
      translations(locale: $locale) { key value locale outdated }
    }
    %s
  }
}""" % PAGE_INFO

TRANSLATIONS_REGISTER = """
mutation ZivoTranslationsRegister($resourceId: ID!, $translations: [TranslationInput!]!) {
  translationsRegister(resourceId: $resourceId, translations: $translations) {
    translations { key locale }
    userErrors { field message code }
  }
}"""

COLLECTIONS = """
query ZivoCollections($cursor: String) {
  collections(first: 100, after: $cursor) {
    nodes { id title handle sources { id __typename } }
    %s
  }
}""" % PAGE_INFO

COLLECTION_CREATE = """
mutation ZivoCollectionCreate($collection: CollectionCreateInput!) {
  collectionCreate(collection: $collection) {
    collection { id title handle sources { id __typename } }
    userErrors { field message }
  }
}"""

ALL_DOCUMENTS = {
    name: value for name, value in globals().items()
    if name.isupper() and isinstance(value, str)
    and name not in ("PAGE_INFO", "MONEY_BAG", "VARIANT_FIELDS", "PRODUCT_FIELDS",
                     "ADDRESS_FIELDS", "CUSTOMER_FIELDS", "ORDER_FIELDS")
}
