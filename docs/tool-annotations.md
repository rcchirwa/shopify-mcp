# MCP tool annotations

Every tool this server registers declares MCP tool annotations (Story 10.73). This file is the
table of record: one row per tool, the four hint values, and the reason for them.
`tests/architecture/test_tool_annotations.py` compares this table with the hints the server
sends in its `tools/list` response, row for row. If you add a tool or change a hint, change
this table in the same commit, or that test fails.

**Annotations are advisory hints, not a security control.** The MCP specification says that a
client must not rely on them for security decisions. They close no SEC item. The control is
still the `confirm` gate inside each write tool. The hints only let a client see, before it
calls a tool, whether the tool can write.

## Rules

The hint sets are defined once, in `src/shopify_mcp/tools/_annotations.py`. Each tool uses one
of them.

1. **The `confirm` rule.** A tool that takes `confirm` declares `readOnlyHint: false`. Without
   `confirm=True` it only returns a preview and changes nothing. But an annotation is static
   and must cover both paths, so it describes the confirmed path: the worst case.
2. **`destructiveHint`** has its MCP meaning. It is `true` when the confirmed path can
   overwrite or remove data that is already in the store, and `false` only when it can only
   add data. It comes from the mutation that the tool sends, not from the tool name.
3. **`idempotentHint`** is `true` when a second call with the same arguments leaves the store
   in the same state as the first call. It is `false` for a tool that creates a new resource:
   a second call makes a second resource, or only a uniqueness refusal stops it. The hint does
   not promise a safe retry that depends on that refusal.
4. **`openWorldHint`** is `true` for every tool, because every tool calls the Shopify Admin API.
5. **Read tools** also declare `destructiveHint: false` and `idempotentHint: true`. Then no
   client uses the specification defaults (destructive, not idempotent) for a tool that only
   reads.

## Hint sets

The "Tools" column is the number of tools that use each set. The test checks it.

| Hint set | readOnly | destructive | idempotent | openWorld | Tools |
|---|---|---|---|---|---|
| `READ_ONLY` | true | false | true | true | 18 |
| `CREATE` | false | false | false | true | 3 |
| `CREATE_OVERWRITE` | false | true | false | true | 1 |
| `ADD` | false | false | true | true | 4 |
| `OVERWRITE` | false | true | true | true | 26 |

## Table

| Tool | Hint set | readOnly | destructive | idempotent | openWorld | Reason |
|---|---|---|---|---|---|---|
| `get_products` | `READ_ONLY` | true | false | true | true | Reads products; no mutation. |
| `get_product` | `READ_ONLY` | true | false | true | true | Reads one product; no mutation. |
| `get_product_description` | `READ_ONLY` | true | false | true | true | Reads `body_html`; no mutation. |
| `get_products_with_descriptions` | `READ_ONLY` | true | false | true | true | Reads `body_html` in bulk; no mutation. |
| `get_product_full` | `READ_ONLY` | true | false | true | true | Reads the full product record; no mutation. |
| `get_products_by_collection` | `READ_ONLY` | true | false | true | true | Reads a collection's products; no mutation. |
| `get_product_collections` | `READ_ONLY` | true | false | true | true | Reads a product's collections; no mutation. |
| `get_product_metafields` | `READ_ONLY` | true | false | true | true | Reads metafields; no mutation. |
| `list_product_media` | `READ_ONLY` | true | false | true | true | Reads product media; no mutation. |
| `get_inventory` | `READ_ONLY` | true | false | true | true | Reads inventory levels; no mutation. |
| `get_collection` | `READ_ONLY` | true | false | true | true | Reads a collection; no mutation. |
| `get_discount_codes` | `READ_ONLY` | true | false | true | true | Reads discount codes; no mutation. |
| `get_orders` | `READ_ONLY` | true | false | true | true | Reads orders; no mutation. |
| `get_order` | `READ_ONLY` | true | false | true | true | Reads one order; no mutation. |
| `list_sales_channels` | `READ_ONLY` | true | false | true | true | Reads publications; no mutation. |
| `get_product_publications` | `READ_ONLY` | true | false | true | true | Reads a product's channels; no mutation. |
| `get_collection_publications` | `READ_ONLY` | true | false | true | true | Reads a collection's channels; no mutation. |
| `list_webhooks` | `READ_ONLY` | true | false | true | true | Reads webhook subscriptions; no mutation. |
| `create_collection` | `CREATE` | false | false | false | true | Creates a new collection; changes no existing data. A repeat is stopped only by the taken-handle refusal. |
| `create_discount_code` | `CREATE` | false | false | false | true | Creates a new discount code; changes no existing data. A repeat is stopped only by Shopify's unique-code rule. |
| `register_webhook` | `CREATE` | false | false | false | true | Creates a new webhook subscription; changes no existing data. Each call asks Shopify for a new subscription. |
| `upload_product_image` | `CREATE_OVERWRITE` | false | true | false | true | Adds a new media item; each call adds one more copy. A `position` other than the end sends `productReorderMedia`, and `position=1` replaces the featured image, which `reorder_product_media` also counts as destructive. |
| `add_product_to_collection` | `ADD` | false | false | true | true | Adds a membership; removes nothing. A repeat leaves the product in the collection. |
| `publish_product_to_channels` | `ADD` | false | false | true | true | Adds channels; removes none. The tool reports a channel that is already published as unchanged. |
| `publish_collection_to_channels` | `ADD` | false | false | true | true | Adds channels; removes none. The tool reports a channel that is already published as unchanged. |
| `resolve_category_metafield_value` | `ADD` | false | false | true | true | With `confirm=True` it can create a metaobject (additive). A repeat finds that metaobject and creates nothing. |
| `update_product_title` | `OVERWRITE` | false | true | true | true | Replaces the title, and the handle when `change_handle=True`. |
| `update_product_description` | `OVERWRITE` | false | true | true | true | Replaces `body_html`. |
| `update_product_seo` | `OVERWRITE` | false | true | true | true | Replaces the SEO title and/or description that the caller supplies. |
| `update_product_tags` | `OVERWRITE` | false | true | true | true | `replace` and `remove` modes delete tags (the worst case). Each mode converges on a repeat. |
| `update_product_status` | `OVERWRITE` | false | true | true | true | Replaces the status. `ARCHIVED` or `DRAFT` takes a live product off sale. |
| `update_variant_inventory_policy` | `OVERWRITE` | false | true | true | true | Replaces each variant's `inventoryPolicy`. |
| `update_product_pricing` | `OVERWRITE` | false | true | true | true | Replaces `price`; `compareAtPrice: None` clears that field. |
| `update_product_category` | `OVERWRITE` | false | true | true | true | Replaces the taxonomy category. |
| `update_product_vendor` | `OVERWRITE` | false | true | true | true | Replaces the vendor; `vendor=None` clears it. |
| `update_product_type` | `OVERWRITE` | false | true | true | true | Replaces `productType`; `""` clears it. |
| `update_variant_image_binding` | `OVERWRITE` | false | true | true | true | Detaches a variant's current media before it appends the new binding, so the old binding is lost. |
| `set_product_metafields` | `OVERWRITE` | false | true | true | true | `metafieldsSet` replaces the value of a metafield that exists. |
| `delete_product_metafields` | `OVERWRITE` | false | true | true | true | Deletes metafields. A repeat is a no-op success (`NOT_FOUND` is treated as done). |
| `update_product_options` | `OVERWRITE` | false | true | true | true | Renames an option and its values; the old names are lost. |
| `update_inventory` | `OVERWRITE` | false | true | true | true | Sets an absolute on-hand quantity (`inventorySetOnHandQuantities`), not a change by a delta. |
| `update_variant_inventory_tracking` | `OVERWRITE` | false | true | true | true | Replaces `InventoryItem.tracked`. |
| `update_variant_inventory_quantity` | `OVERWRITE` | false | true | true | true | Sets an absolute on-hand quantity at each matched location; the old quantity is lost. |
| `update_product_media` | `OVERWRITE` | false | true | true | true | Replaces the alt text. |
| `reorder_product_media` | `OVERWRITE` | false | true | true | true | Replaces the display order, including which item is featured. |
| `delete_product_media` | `OVERWRITE` | false | true | true | true | Deletes media. A repeat finds nothing more to delete. |
| `update_collection` | `OVERWRITE` | false | true | true | true | Replaces the title and/or description. |
| `remove_product_from_collection` | `OVERWRITE` | false | true | true | true | Removes a membership. A repeat leaves the product out of the collection. |
| `unpublish_product_from_channels` | `OVERWRITE` | false | true | true | true | Removes channels. On a DRAFT product it also removes an assigned channel. |
| `unpublish_collection_from_channels` | `OVERWRITE` | false | true | true | true | Removes channels. Removing Online Store makes the collection page return 404. |
| `set_product_publications` | `OVERWRITE` | false | true | true | true | Unpublishes every channel that is not in the list. |
| `delete_webhook` | `OVERWRITE` | false | true | true | true | Deletes a webhook subscription. |
