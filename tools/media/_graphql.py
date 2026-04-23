"""GraphQL query and mutation strings used by the media tools."""

GET_PRODUCT_MEDIA = """
query GetProductMedia($id: ID!) {
  product(id: $id) {
    id
    title
    media(first: 100) {
      nodes {
        id
        alt
        mediaContentType
        status
        preview { image { url } }
      }
      pageInfo { hasNextPage }
    }
  }
}
"""

# Targeted per-media read used by _poll_media_ready. Reading just the one node
# beats refetching the full media list every tick on media-heavy products.
# Every media type exposes `status` and `preview`, but only through the
# per-type inline fragments — `node` itself returns the generic `Node`
# interface, which has neither field.
GET_MEDIA_STATUS = """
query GetMediaStatus($id: ID!) {
  node(id: $id) {
    ... on MediaImage { id status preview { image { url } } }
    ... on Video { id status preview { image { url } } }
    ... on Model3d { id status preview { image { url } } }
    ... on ExternalVideo { id status preview { image { url } } }
  }
}
"""

STAGED_UPLOADS_CREATE = """
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""

PRODUCT_CREATE_MEDIA = """
mutation ProductCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media {
      id
      alt
      mediaContentType
      status
      preview { image { url } }
    }
    mediaUserErrors { field message }
  }
}
"""

PRODUCT_REORDER_MEDIA = """
mutation ProductReorderMedia($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job { id done }
    mediaUserErrors { field message }
    userErrors { field message }
  }
}
"""

PRODUCT_UPDATE_MEDIA = """
mutation ProductUpdateMedia($productId: ID!, $media: [UpdateMediaInput!]!) {
  productUpdateMedia(productId: $productId, media: $media) {
    media { id alt }
    mediaUserErrors { field message }
  }
}
"""

PRODUCT_DELETE_MEDIA = """
mutation ProductDeleteMedia($productId: ID!, $mediaIds: [ID!]!) {
  productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
    deletedMediaIds
    product { id }
    mediaUserErrors { field message }
  }
}
"""
