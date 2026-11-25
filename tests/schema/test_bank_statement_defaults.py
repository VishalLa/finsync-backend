from schema import BankStatement


def test_blank_bank_currency_fields_are_float_zeroes() -> None:
    bank = BankStatement(
        row_index=1,
        bank_name="Regression Bank",
        template_version="1",
        debit_amount="",
        credit_amount=None,
    )

    assert bank.debit_amount == 0.0
    assert bank.credit_amount == 0.0
    assert isinstance(bank.debit_amount, float)
    assert isinstance(bank.credit_amount, float)
