Analyse finances interactively.

Arguments: $ARGUMENTS

## Process

If the user asked a specific question, answer it using the appropriate tools below. If no question was asked, give a quick financial status:

1. Call `finance_summary` with period "this_month"
2. Show: income, expenses, net savings, savings rate, categorisation coverage
3. If coverage < 95%, mention uncategorised count and offer to categorise

## Tool Routing

Match the user's question to the right tool:

| Question type | Tool(s) |
|---------------|---------|
| "Where am I spending?" / spending breakdown | `finance_categories` then `finance_top_merchants` for detail |
| "How has X changed?" / month-over-month | `finance_compare` with appropriate periods |
| "What subscriptions do I have?" | `finance_subscriptions` |
| "Show my accounts" / account breakdown | `finance_accounts` |
| "How much did I spend on [X]?" | `finance_transactions` with search or category filter |
| "Spending trends" / over time | `finance_trends` |
| "What needs categorising?" | `finance_uncategorized`, then suggest rules via `finance_add_rule` |
| Specific merchant or payee | `finance_top_merchants` with category filter, or `finance_transactions` with search |

## Conventions

- Currency is Euro
- Use Irish date format (DD/MM/YYYY) in prose
- Round amounts to 2 decimal places
- When showing comparisons, highlight the biggest changes
- Transfers between own accounts are excluded from all analytics

## Follow-ups

The user may ask follow-up questions. Continue the analysis conversationally, calling additional tools as needed. You can combine multiple tool calls to build a complete picture.

## Examples

- `/finance` — quick status for this month
- `/finance where am I spending the most?` — category + merchant breakdown
- `/finance how has grocery spending changed?` — compare recent periods for Groceries
- `/finance subscriptions` — list detected recurring charges
