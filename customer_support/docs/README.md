# Customer Support AI Module

## Purpose

This folder stores customer support RAG data and maintenance scripts.

## Paths

- Data file:
  - `/home/mamun/app/customer_support/data/customer_support_faq.jsonl`

- Upsert script:
  - `/home/mamun/app/customer_support/scripts/upsert_customer_support.py`

- Test script:
  - `/home/mamun/app/customer_support/scripts/test_customer_support_search.py`

## Qdrant

- URL: `http://10.50.1.29:31635`
- Collection: `customer_support`
- Vector size: `768`
- Distance: `Cosine`

## Ollama

- URL: `http://192.168.1.144:31134`
- Embedding model: `nomic-embed-text`
- Chat model: `aya-23-8b:q5`

## Maintenance Commands

Create collection:

```bash
curl -X PUT "http://10.50.1.29:31635/collections/customer_support" \
  -H "Content-Type: application/json" \
  -d '{
    "vectors": {
      "size": 768,
      "distance": "Cosine"
    }
  }'
