# beancount-limit-enforcer
Creates beancount errors when balances hit maximum and/or minimum on accounts.

Some accounts may have limits such as credit cards.

## Usage
Add the following to the beancount file:
```beancount
plugin "beancount-limit-enforcer.value_warn_balance"
```

Then add the max or min values to the open account statements.

Example
```beancount
2000-01-01 open Liabilities:CreditCard:Card1 USD
  min_value: "-13,000.00 USD"

2000-01-01 open Assets:AccountsReceivable:TimeOff PTOHR
  max_value: "8.0 PTOHR"
```

## Known errors
Unable to currently have both set.
