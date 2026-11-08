"""Tests for OOP fundamentals: value objects, containers, accounts, dataclasses, ABCs, ordering."""

from __future__ import annotations

import json
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest


class TestPart1Money:
    def test_construction_and_read_only_attributes(self, main: ModuleType) -> None:
        money = main.Money("10.50", "USD")
        assert money.amount == Decimal("10.50"), "amount should be stored as a Decimal exactly as given"
        assert money.currency == "USD", "currency should be exposed unchanged"
        with pytest.raises(AttributeError):
            money.amount = Decimal("1")  # type: ignore[misc]

    def test_input_validation(self, main: ModuleType) -> None:
        assert main.Money(10, "USD") == main.Money("10.00", "USD"), "int and str amounts should agree"
        with pytest.raises(TypeError):
            main.Money(10.5, "USD")  # floats are forbidden; the message should steer to Decimal/str
        with pytest.raises(ValueError):
            main.Money("abc", "USD")
        with pytest.raises(ValueError):
            main.Money("NaN", "USD")
        with pytest.raises(ValueError):
            main.Money("1", "usd")
        with pytest.raises(TypeError):
            main.Money("1", 840)  # type: ignore[arg-type]

    def test_add_sub_same_currency_and_mismatch(self, main: ModuleType) -> None:
        a = main.Money("10.00", "USD")
        b = main.Money("2.50", "USD")
        assert a + b == main.Money("12.50", "USD"), "addition should sum amounts"
        assert a - b == main.Money("7.50", "USD"), "subtraction should subtract amounts"
        eur = main.Money("1.00", "EUR")
        with pytest.raises(main.CurrencyMismatchError):
            _ = a + eur
        assert issubclass(main.CurrencyMismatchError, ValueError), "mismatch error should be a ValueError"
        with pytest.raises(TypeError):
            _ = a + 1  # type: ignore[operator]

    def test_equality_hash_consistency(self, main: ModuleType) -> None:
        a = main.Money(1, "USD")
        b = main.Money("1.00", "USD")
        assert a == b, "amounts equal numerically should compare equal regardless of scale"
        assert hash(a) == hash(b), "equal objects must hash equally"
        assert len({a, b}) == 1, "set should collapse equal values"
        assert main.Money(1, "USD") != main.Money(1, "EUR"), "different currency is not equal"
        assert (main.Money(1, "USD") == 1) is False, "comparison with a non-Money must be False, not an error"

    def test_ordering(self, main: ModuleType) -> None:
        values = [main.Money("3.00", "USD"), main.Money("1.50", "USD"), main.Money("2.00", "USD")]
        assert [m.amount for m in sorted(values)] == [Decimal("1.50"), Decimal("2.00"), Decimal("3.00")]
        assert main.Money("1", "USD") < main.Money("2", "USD"), "< should order by amount"
        assert main.Money("2", "USD") >= main.Money("2.00", "USD"), ">= should accept equal amounts"
        with pytest.raises(main.CurrencyMismatchError):
            _ = main.Money("1", "USD") < main.Money("1", "EUR")
        with pytest.raises(TypeError):
            _ = main.Money("1", "USD") < 5  # type: ignore[operator]

    def test_sum_works_via_radd(self, main: ModuleType) -> None:
        moneys = [main.Money("1.00", "USD"), main.Money("2.50", "USD"), main.Money("0.25", "USD")]
        assert sum(moneys) == main.Money("3.75", "USD"), "sum() starts from 0 and needs __radd__ to accept it"
        assert 0 + main.Money("4", "USD") == main.Money("4", "USD"), "0 + Money should return the money"
        with pytest.raises(TypeError):
            _ = 5 + main.Money("4", "USD")  # type: ignore[operator]
        with pytest.raises(main.CurrencyMismatchError):
            sum([main.Money("1", "USD"), main.Money("1", "EUR")])

    def test_multiply_negate_bool_repr_str(self, main: ModuleType) -> None:
        m = main.Money("2.00", "USD")
        assert m * 3 == main.Money("6.00", "USD"), "int multiplication"
        assert 3 * m == main.Money("6.00", "USD"), "__rmul__ must work"
        assert m * Decimal("0.5") == main.Money("1.00", "USD"), "Decimal multiplication"
        assert -m == main.Money("-2.00", "USD"), "unary minus negates"
        with pytest.raises(TypeError):
            _ = m * 1.5  # type: ignore[operator]
        with pytest.raises(TypeError):
            _ = m * m  # type: ignore[operator]
        assert bool(main.Money("0.00", "USD")) is False, "zero money is falsy"
        assert bool(m) is True, "non-zero money is truthy"
        assert repr(main.Money("10.50", "USD")) == "Money('10.50', 'USD')", "repr should be eval-style"
        assert str(main.Money("10.50", "USD")) == "10.50 USD", "str should be 'amount CURRENCY'"
        assert str(main.Money("1E+2", "USD")) == "100 USD", "str should avoid scientific notation"

    def test_rounded_half_even_with_minor_units(self, main: ModuleType) -> None:
        assert main.Money("1.005", "USD").rounded() == main.Money("1.00", "USD"), "half-even: 0 is even"
        assert main.Money("1.015", "USD").rounded() == main.Money("1.02", "USD"), "half-even: 1 is odd, round up"
        assert main.Money("1.006", "USD").rounded() == main.Money("1.01", "USD"), "above half rounds up"
        assert main.Money("100.5", "JPY").rounded() == main.Money("100", "JPY"), "JPY has zero minor units"
        assert main.Money("101.5", "JPY").rounded() == main.Money("102", "JPY"), "half-even on JPY"
        assert main.Money("1.2345", "KWD").rounded() == main.Money("1.234", "KWD"), "KWD has three minor units"

    def test_allocate_distributes_remainder(self, main: ModuleType) -> None:
        parts = main.Money("10.00", "USD").allocate(3)
        assert [p.amount for p in parts] == [Decimal("3.34"), Decimal("3.33"), Decimal("3.33")], (
            "extra minor units go to the first shares"
        )
        assert sum(p.amount for p in parts) == Decimal("10.00"), "shares must sum exactly to the original"
        assert all(isinstance(p, main.Money) for p in parts), "each share must be a Money"

    def test_allocate_negative_and_jpy(self, main: ModuleType) -> None:
        parts = main.Money("-10.00", "USD").allocate(3)
        assert [p.amount for p in parts] == [Decimal("-3.34"), Decimal("-3.33"), Decimal("-3.33")], (
            "allocate by magnitude, then apply the sign to each share"
        )
        jpy = main.Money("100", "JPY").allocate(3)
        assert [p.amount for p in jpy] == [Decimal("34"), Decimal("33"), Decimal("33")], "JPY uses zero decimals"

    def test_allocate_validation(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.Money("1.005", "USD").allocate(2)  # more precision than minor units
        with pytest.raises(ValueError):
            main.Money("1.00", "USD").allocate(0)
        with pytest.raises(TypeError):
            main.Money("1.00", "USD").allocate(True)  # type: ignore[arg-type]


class TestPart2Product:
    def test_constructor_and_read_only_sku(self, main: ModuleType) -> None:
        price = main.Money("9.99", "USD")
        product = main.Product("WIDGET-1", "  Widget  ", price, quantity=3)
        assert product.sku == "WIDGET-1", "sku should be stored as given"
        assert product.name == "Widget", "name should be stored stripped"
        assert product.price == price, "price should be stored"
        assert product.quantity == 3, "quantity should be stored"
        with pytest.raises(AttributeError):
            product.sku = "OTHER-1"  # type: ignore[misc]

    def test_sku_validation(self, main: ModuleType) -> None:
        price = main.Money("1", "USD")
        with pytest.raises(ValueError):
            main.Product("ab", "x", price)  # too short
        with pytest.raises(ValueError):
            main.Product("lower-case1", "x", price)  # lowercase not allowed
        with pytest.raises(ValueError):
            main.Product("A" * 17, "x", price)  # too long
        with pytest.raises(TypeError):
            main.Product(1234, "x", price)  # type: ignore[arg-type]

    def test_setters_validate(self, main: ModuleType) -> None:
        product = main.Product("WIDGET-2", "Widget", main.Money("1", "USD"))
        product.name = "  Gadget "
        assert product.name == "Gadget", "name setter should strip"
        with pytest.raises(ValueError):
            product.name = "   "
        with pytest.raises(ValueError):
            product.name = "x" * 101
        with pytest.raises(ValueError):
            product.quantity = -1
        with pytest.raises(TypeError):
            product.quantity = True
        with pytest.raises(TypeError):
            product.quantity = 2.0  # type: ignore[assignment]
        with pytest.raises(TypeError):
            product.price = 10  # type: ignore[assignment]

    def test_restock_and_in_stock(self, main: ModuleType) -> None:
        product = main.Product("WIDGET-3", "Widget", main.Money("1", "USD"))
        assert product.in_stock is False, "quantity 0 is out of stock"
        product.restock(4)
        assert product.quantity == 4, "restock should add to quantity"
        assert product.in_stock is True, "positive quantity is in stock"
        with pytest.raises(ValueError):
            product.restock(0)

    def test_class_counter_counts_all_instances_including_subclasses(self, main: ModuleType) -> None:
        class Gadget(main.Product):
            pass

        price = main.Money("1", "USD")
        before = main.Product.total_created
        main.Product("WIDGET-4", "A", price)
        Gadget("GADGET-5", "B", price)
        assert main.Product.total_created == before + 2, "subclass instances must also increment the class counter"
        assert "total_created" not in vars(Gadget("GADGET-6", "C", price)), (
            "the counter must not be shadowed by an instance attribute"
        )
        assert main.Product.total_created == before + 3, "every construction counts"

    def test_failed_construction_does_not_count(self, main: ModuleType) -> None:
        price = main.Money("1", "USD")
        before = main.Product.total_created
        with pytest.raises(ValueError):
            main.Product("bad", "x", price)
        assert main.Product.total_created == before, "a constructor that raises must not increment the counter"

    def test_count_created_classmethod(self, main: ModuleType) -> None:
        class Gadget(main.Product):
            pass

        assert main.Product.count_created() == main.Product.total_created, "classmethod returns the class counter"
        assert Gadget.count_created() == main.Product.total_created, "classmethod works through subclasses too"


class TestPart3Accounts:
    def test_base_account_deposit_withdraw(self, main: ModuleType) -> None:
        account = main.Account("Ada")
        assert account.owner == "Ada", "owner should be stored"
        assert account.balance == main.Money.zero("USD"), "default balance is zero USD"
        assert account.deposit(main.Money("50", "USD")) == main.Money("50", "USD"), "deposit returns new balance"
        assert account.withdraw(main.Money("20", "USD")) == main.Money("30", "USD"), "withdraw returns new balance"
        with pytest.raises(main.InsufficientFundsError):
            account.withdraw(main.Money("31", "USD"))
        assert account.balance == main.Money("30", "USD"), "failed withdrawal must not change the balance"
        assert account.withdraw(main.Money("30", "USD")) == main.Money.zero("USD"), "exact balance is allowed"

    def test_deposit_withdraw_validation(self, main: ModuleType) -> None:
        account = main.Account("Ada", main.Money("10", "USD"))
        with pytest.raises(ValueError):
            account.deposit(main.Money("0", "USD"))
        with pytest.raises(ValueError):
            account.withdraw(main.Money("-1", "USD"))
        with pytest.raises(TypeError):
            account.deposit(5)  # type: ignore[arg-type]
        with pytest.raises(main.CurrencyMismatchError):
            account.deposit(main.Money("5", "EUR"))

    def test_checking_overdraft(self, main: ModuleType) -> None:
        account = main.CheckingAccount("Ada", main.Money("50", "USD"), main.Money("100", "USD"))
        assert account.overdraft_limit == main.Money("100", "USD"), "overdraft limit should be exposed"
        assert account.withdraw(main.Money("150", "USD")) == main.Money("-100", "USD"), (
            "balance may go negative down to minus the overdraft limit"
        )
        with pytest.raises(main.InsufficientFundsError):
            account.withdraw(main.Money("1", "USD"))
        assert account.balance == main.Money("-100", "USD"), "rejected withdrawal leaves balance unchanged"
        account.deposit(main.Money("100", "USD"))
        assert account.balance == main.Money.zero("USD"), "deposits reduce the overdraft"

    def test_checking_overdraft_defaults_and_validation(self, main: ModuleType) -> None:
        plain = main.CheckingAccount("Ada", main.Money("10", "USD"))
        assert plain.overdraft_limit == main.Money.zero("USD"), "default overdraft is zero"
        with pytest.raises(main.InsufficientFundsError):
            plain.withdraw(main.Money("11", "USD"))
        with pytest.raises(ValueError):
            main.CheckingAccount("Ada", main.Money("10", "USD"), main.Money("-1", "USD"))
        assert isinstance(plain, main.Account), "CheckingAccount is an Account"

    def test_savings_withdrawal_limit(self, main: ModuleType) -> None:
        account = main.SavingsAccount("Ada", main.Money("100", "USD"), max_withdrawals=2)
        assert account.max_withdrawals == 2, "max_withdrawals should be exposed"
        account.withdraw(main.Money("10", "USD"))
        account.deposit(main.Money("5", "USD"))
        account.withdraw(main.Money("10", "USD"))
        assert account.withdrawals_this_period == 2, "deposits must not count; successful withdrawals do"
        with pytest.raises(main.WithdrawalLimitExceeded):
            account.withdraw(main.Money("1", "USD"))
        account.reset_period()
        assert account.withdrawals_this_period == 0, "reset_period zeroes the counter"
        account.withdraw(main.Money("1", "USD"))
        assert account.balance == main.Money("84", "USD"), "withdrawals after reset succeed"

    def test_failed_withdrawal_does_not_consume_quota(self, main: ModuleType) -> None:
        account = main.SavingsAccount("Ada", main.Money("10", "USD"), max_withdrawals=1)
        with pytest.raises(main.InsufficientFundsError):
            account.withdraw(main.Money("50", "USD"))
        assert account.withdrawals_this_period == 0, "an insufficient-funds failure is not a withdrawal"
        account.withdraw(main.Money("10", "USD"))
        assert account.balance == main.Money.zero("USD"), "the quota was still available"

    def test_savings_constructor_rules(self, main: ModuleType) -> None:
        assert main.SavingsAccount("Ada").max_withdrawals == 3, "default limit is 3"
        with pytest.raises(TypeError):
            main.SavingsAccount("Ada", None, 2)  # type: ignore[misc]  # max_withdrawals is keyword-only
        with pytest.raises(ValueError):
            main.SavingsAccount("Ada", max_withdrawals=-1)
        with pytest.raises(TypeError):
            main.SavingsAccount("Ada", max_withdrawals=True)  # type: ignore[arg-type]
        zero = main.SavingsAccount("Ada", main.Money("5", "USD"), max_withdrawals=0)
        with pytest.raises(main.WithdrawalLimitExceeded):
            zero.withdraw(main.Money("1", "USD"))

    def test_polymorphic_withdraw_through_base_type(self, main: ModuleType) -> None:
        accounts: list[Any] = [
            main.CheckingAccount("Ada", main.Money("0", "USD"), main.Money("20", "USD")),
            main.SavingsAccount("Grace", main.Money("20", "USD")),
        ]
        for account in accounts:
            assert isinstance(account, main.Account), "every account is an Account"
            account.withdraw(main.Money("20", "USD"))
        assert [a.balance for a in accounts] == [main.Money("-20", "USD"), main.Money.zero("USD")], (
            "both subclasses must honour base withdraw semantics via super()"
        )


class TestPart4ShoppingCart:
    def test_add_len_and_getitem(self, main: ModuleType) -> None:
        widget = main.Product("WIDGET-1", "Widget", main.Money("9.99", "USD"))
        cart = main.ShoppingCart()
        cart.add(widget, 2)
        cart.add(widget)
        assert len(cart) == 3, "len counts total units"
        assert cart["WIDGET-1"] == 3, "quantities accumulate per sku"
        with pytest.raises(KeyError):
            _ = cart["MISSING-1"]

    def test_contains_accepts_sku_or_product(self, main: ModuleType) -> None:
        widget = main.Product("WIDGET-1", "Widget", main.Money("9.99", "USD"))
        cart = main.ShoppingCart()
        cart.add(widget)
        assert "WIDGET-1" in cart, "sku string membership"
        assert widget in cart, "a Product is checked by its sku"
        assert "MISSING-1" not in cart, "absent sku"
        assert 42 not in cart, "unsupported types are simply not contained"

    def test_iter_insertion_order(self, main: ModuleType) -> None:
        a = main.Product("AAAA-1", "A", main.Money("1", "USD"))
        b = main.Product("BBBB-1", "B", main.Money("1", "USD"))
        cart = main.ShoppingCart()
        cart.add(b, 2)
        cart.add(a)
        cart.add(b)
        assert list(cart) == [("BBBB-1", 3), ("AAAA-1", 1)], "iteration yields (sku, qty) in insertion order"

    def test_truthiness(self, main: ModuleType) -> None:
        cart = main.ShoppingCart()
        assert not cart, "an empty cart is falsy"
        assert len(cart) == 0, "empty cart has zero units"
        cart.add(main.Product("AAAA-1", "A", main.Money("1", "USD")))
        assert cart, "a non-empty cart is truthy"

    def test_remove_rules(self, main: ModuleType) -> None:
        widget = main.Product("WIDGET-1", "Widget", main.Money("9.99", "USD"))
        cart = main.ShoppingCart()
        cart.add(widget, 3)
        cart.remove("WIDGET-1")
        assert cart["WIDGET-1"] == 2, "remove defaults to one unit"
        cart.remove("WIDGET-1", 2)
        assert "WIDGET-1" not in cart, "removing every unit deletes the line"
        with pytest.raises(KeyError):
            cart.remove("WIDGET-1")
        cart.add(widget, 1)
        with pytest.raises(ValueError):
            cart.remove("WIDGET-1", 2)
        with pytest.raises(ValueError):
            cart.remove("WIDGET-1", 0)

    def test_total_uses_money_arithmetic(self, main: ModuleType) -> None:
        widget = main.Product("WIDGET-1", "Widget", main.Money("9.99", "USD"))
        penny = main.Product("PENNY-1", "Penny", main.Money("0.01", "USD"))
        cart = main.ShoppingCart("USD")
        assert cart.total() == main.Money.zero("USD"), "empty cart total is zero Money"
        cart.add(widget, 2)
        cart.add(penny)
        assert cart.total() == main.Money("19.99", "USD"), "total is price * quantity summed"
        assert isinstance(cart.total(), main.Money), "total returns Money"

    def test_currency_and_add_validation(self, main: ModuleType) -> None:
        cart = main.ShoppingCart("EUR")
        assert cart.currency == "EUR", "currency should be exposed"
        usd = main.Product("WIDGET-1", "Widget", main.Money("9.99", "USD"))
        with pytest.raises(main.CurrencyMismatchError):
            cart.add(usd)
        eur = main.Product("WIDGET-2", "Widget", main.Money("9.99", "EUR"))
        with pytest.raises(ValueError):
            cart.add(eur, 0)
        with pytest.raises(TypeError):
            cart.add(eur, True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            cart.add("WIDGET-2")  # type: ignore[arg-type]

    def test_repr(self, main: ModuleType) -> None:
        cart = main.ShoppingCart("USD")
        cart.add(main.Product("WIDGET-1", "Widget", main.Money("9.99", "USD")), 3)
        assert repr(cart) == "ShoppingCart(currency='USD', items=3)", "repr shows currency and total units"


class TestPart5Employee:
    def test_fields_and_post_init_normalisation(self, main: ModuleType) -> None:
        emp = main.Employee(
            employee_id=7,
            name="  Ada Lovelace ",
            email="  ADA@Example.COM ",
            department="Engineering",
            salary=main.Money("95000.00", "USD"),
            skills=[" Python", "go", "PYTHON", "", "go "],
        )
        assert emp.name == "Ada Lovelace", "name should be stripped"
        assert emp.email == "ada@example.com", "email should be normalised"
        assert emp.skills == ["python", "go"], "skills are stripped, lowercased, de-duplicated, empties dropped"

    def test_default_list_is_not_shared(self, main: ModuleType) -> None:
        salary = main.Money("1", "USD")
        a = main.Employee(1, "A", "a@x.io", "Ops", salary)
        b = main.Employee(2, "B", "b@x.io", "Ops", salary)
        a.skills.append("sql")
        assert b.skills == [], "default_factory must give each instance its own list"

    def test_normalize_email_staticmethod(self, main: ModuleType) -> None:
        assert main.Employee.normalize_email(" X@Y.Org ") == "x@y.org", "callable on the class"
        emp = main.Employee(1, "A", "a@x.io", "Ops", main.Money("1", "USD"))
        assert emp.normalize_email("Z@W.net") == "z@w.net", "callable on an instance"
        for bad in ["no-at-sign", "a@b@c", "@x.com", "a@", "a b@x.com"]:
            with pytest.raises(ValueError):
                main.Employee.normalize_email(bad)
        with pytest.raises(TypeError):
            main.Employee.normalize_email(None)  # type: ignore[arg-type]

    def test_from_dict(self, main: ModuleType) -> None:
        emp = main.Employee.from_dict(
            {
                "employee_id": "1042",
                "name": "Grace Hopper",
                "email": "grace@navy.mil",
                "department": "Compilers",
                "salary": "120000.50",
                "currency": "USD",
                "skills": ["cobol"],
                "ignored": True,
            }
        )
        assert emp.employee_id == 1042, "string ids are coerced to int"
        assert emp.salary == main.Money("120000.50", "USD"), "salary and currency combine into Money"
        assert emp.skills == ["cobol"], "skills come from the dict"
        bare = main.Employee.from_dict(
            {"employee_id": 1, "name": "A", "email": "a@x.io", "department": "Ops", "salary": 1, "currency": "USD"}
        )
        assert bare.skills == [], "missing skills default to an empty list"

    def test_from_dict_errors(self, main: ModuleType) -> None:
        with pytest.raises(KeyError):
            main.Employee.from_dict({"employee_id": 1, "name": "A", "email": "a@x.io", "department": "Ops"})
        with pytest.raises(TypeError):
            main.Employee.from_dict(
                {
                    "employee_id": 1,
                    "name": "A",
                    "email": "a@x.io",
                    "department": "Ops",
                    "salary": 1,
                    "currency": "USD",
                    "skills": "python",
                }
            )

    def test_from_csv_row(self, main: ModuleType) -> None:
        emp = main.Employee.from_csv_row(
            ["1043", "Linus Torvalds", "Linus@Kernel.org", "Platform", "99000", "USD", "C; git;C"]
        )
        assert emp.employee_id == 1043, "first column is the id"
        assert emp.email == "linus@kernel.org", "email is normalised on the CSV path too"
        assert emp.skills == ["c", "git"], "semicolon-separated skills, de-duplicated"
        empty_skills = main.Employee.from_csv_row(["1", "A", "a@x.io", "Ops", "1", "USD", ""])
        assert empty_skills.skills == [], "an empty skills column means no skills"
        with pytest.raises(ValueError):
            main.Employee.from_csv_row(["1", "A", "a@x.io", "Ops", "1", "USD"])

    def test_validation_rejects_bad_fields(self, main: ModuleType) -> None:
        salary = main.Money("1", "USD")
        with pytest.raises(ValueError):
            main.Employee(0, "A", "a@x.io", "Ops", salary)
        with pytest.raises(TypeError):
            main.Employee(True, "A", "a@x.io", "Ops", salary)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            main.Employee(1, "   ", "a@x.io", "Ops", salary)
        with pytest.raises(ValueError):
            main.Employee(1, "A", "a@x.io", "Ops", main.Money("-1", "USD"))
        with pytest.raises(TypeError):
            main.Employee(1, "A", "a@x.io", "Ops", 100)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            main.Employee(1, "A", "a@x.io", "Ops", salary, "python")  # type: ignore[arg-type]

    def test_equality_and_subclass_alt_constructors(self, main: ModuleType) -> None:
        class Contractor(main.Employee):
            pass

        a = main.Employee.from_csv_row(["5", "Ada", "ada@x.io", "Eng", "10", "USD", "py"])
        b = main.Employee(5, "Ada", "ada@x.io", "Eng", main.Money("10", "USD"), ["py"])
        assert a == b, "dataclass equality compares fields"
        contractor = Contractor.from_dict(
            {"employee_id": 9, "name": "C", "email": "c@x.io", "department": "Ops", "salary": 1, "currency": "USD"}
        )
        assert isinstance(contractor, Contractor), "alternative constructors must respect the calling subclass"


class TestPart6Notifiers:
    def test_abc_cannot_be_instantiated(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.Notifier()  # type: ignore[abstract]

    def test_subclass_must_implement_all_abstract_members(self, main: ModuleType) -> None:
        class OnlySend(main.Notifier):
            def send(self, recipient: str, message: str) -> None:
                return None

        class OnlyChannel(main.Notifier):
            @property
            def channel_name(self) -> str:
                return "x"

        with pytest.raises(TypeError):
            OnlySend()  # type: ignore[abstract]
        with pytest.raises(TypeError):
            OnlyChannel()  # type: ignore[abstract]
        email = main.EmailNotifier(lambda to, body: None)
        assert isinstance(email, main.Notifier), "concrete notifiers are Notifiers"
        assert email.describe() == "email notifier", "describe() uses channel_name"

    def test_email_notifier(self, main: ModuleType) -> None:
        sent: list[tuple[str, str]] = []
        notifier = main.EmailNotifier(lambda to, body: sent.append((to, body)))
        assert notifier.channel_name == "email", "email channel name"
        notifier.send("ada@example.com", "Hello")
        assert sent == [("ada@example.com", "Hello")], "transport receives (address, body)"
        with pytest.raises(ValueError):
            notifier.send("not-an-address", "Hello")
        with pytest.raises(ValueError):
            notifier.send("ada@example.com", "   ")
        assert len(sent) == 1, "invalid sends must not reach the transport"

    def test_sms_notifier_rules(self, main: ModuleType) -> None:
        sent: list[tuple[str, str]] = []
        notifier = main.SMSNotifier(lambda to, body: sent.append((to, body)))
        assert notifier.channel_name == "sms", "sms channel name"
        notifier.send("+15551234567", "ok")
        assert sent == [("+15551234567", "ok")], "transport receives (number, text)"
        for bad in ["5551234567", "+05551234567", "+1", "+1 555 123 4567"]:
            with pytest.raises(ValueError):
                notifier.send(bad, "ok")
        notifier.send("+15551234567", "x" * 160)
        with pytest.raises(ValueError):
            notifier.send("+15551234567", "x" * 161)
        assert main.SMSNotifier.MAX_LENGTH == 160, "MAX_LENGTH is 160"

    def test_webhook_posts_sorted_json(self, main: ModuleType) -> None:
        posted: list[tuple[str, bytes]] = []
        status = [200]

        def post(url: str, body: bytes) -> int:
            posted.append((url, body))
            return status[0]

        notifier = main.WebhookNotifier("https://hooks.example.com/notify", post)
        assert notifier.channel_name == "webhook", "webhook channel name"
        notifier.send("user-42", "shipped")
        url, body = posted[0]
        assert url == "https://hooks.example.com/notify", "the endpoint is passed to post"
        assert body == b'{"message": "shipped", "recipient": "user-42"}', "UTF-8 JSON with sorted keys"
        assert json.loads(body) == {"message": "shipped", "recipient": "user-42"}, "body round-trips as JSON"

        status[0] = 204
        notifier.send("user-42", "still fine")
        status[0] = 500
        with pytest.raises(main.NotificationError):
            notifier.send("user-42", "boom")
        with pytest.raises(ValueError):
            main.WebhookNotifier("http://insecure.example.com", post)

    def test_dispatcher_sends_only_requested_channels(self, main: ModuleType) -> None:
        email_sent: list[str] = []
        sms_sent: list[str] = []
        dispatcher = main.NotificationDispatcher(
            [
                main.EmailNotifier(lambda to, body: email_sent.append(to)),
                main.SMSNotifier(lambda to, body: sms_sent.append(to)),
            ]
        )
        assert dispatcher.channels == ("email", "sms"), "channels follow construction order"
        assert len(dispatcher) == 2, "len is the number of notifiers"
        results = dispatcher.dispatch("Your order shipped", {"email": "ada@example.com", "sms": "+15551234567"})
        assert results == {"email": None, "sms": None}, "successful sends map to None"
        assert email_sent == ["ada@example.com"] and sms_sent == ["+15551234567"], "each channel got its recipient"

        email_sent.clear()
        sms_sent.clear()
        dispatcher.dispatch("Only email", {"email": "ada@example.com"})
        assert sms_sent == [], "channels not in the mapping are not contacted"

    def test_dispatcher_collects_failures(self, main: ModuleType) -> None:
        email_sent: list[str] = []

        def broken_sms(to: str, body: str) -> None:
            raise RuntimeError("carrier down")

        dispatcher = main.NotificationDispatcher(
            [
                main.SMSNotifier(broken_sms),
                main.EmailNotifier(lambda to, body: email_sent.append(to)),
            ]
        )
        results = dispatcher.dispatch("hi", {"sms": "+15551234567", "email": "ada@example.com"})
        assert isinstance(results["sms"], RuntimeError), "the failing channel records its exception"
        assert results["email"] is None, "a failure must not stop the other channels"
        assert email_sent == ["ada@example.com"], "email was still delivered"
        assert list(results) == ["sms", "email"], "result keys follow the recipients mapping order"

    def test_dispatcher_validation(self, main: ModuleType) -> None:
        sent: list[str] = []
        dispatcher = main.NotificationDispatcher([main.EmailNotifier(lambda to, body: sent.append(to))])
        with pytest.raises(KeyError):
            dispatcher.dispatch("hi", {"email": "ada@example.com", "pigeon": "coop-7"})
        assert sent == [], "an unknown channel is rejected before anything is sent"
        with pytest.raises(ValueError):
            main.NotificationDispatcher(
                [
                    main.EmailNotifier(lambda to, body: None),
                    main.EmailNotifier(lambda to, body: None),
                ]
            )
        with pytest.raises(TypeError):
            main.NotificationDispatcher(["not a notifier"])  # type: ignore[list-item]


class TestPart7Version:
    def test_parse_and_str_roundtrip(self, main: ModuleType) -> None:
        v = main.Version.parse("1.2.3")
        assert (v.major, v.minor, v.patch) == (1, 2, 3), "core components"
        assert v.prerelease == "" and v.build == "", "no prerelease or build by default"
        assert str(main.Version.parse("v1.2.3")) == "1.2.3", "leading v is accepted but not kept"
        full = main.Version.parse("1.2.3-rc.1+build.5")
        assert str(full) == "1.2.3-rc.1+build.5", "str is canonical and keeps prerelease and build"
        assert full.prerelease == "rc.1" and full.build == "build.5", "prerelease and build are exposed"
        assert repr(v) == "Version.parse('1.2.3')", "repr is eval-style"

    def test_parse_rejects_invalid(self, main: ModuleType) -> None:
        for bad in ["1.2", "01.2.3", "1.2.3-", "1.2.3+", "1.2.3-01", "a.b.c", "1.2.3-rc..1"]:
            with pytest.raises(ValueError):
                main.Version.parse(bad)
        with pytest.raises(TypeError):
            main.Version.parse(123)  # type: ignore[arg-type]

    def test_constructor_validation(self, main: ModuleType) -> None:
        assert str(main.Version(2)) == "2.0.0", "minor and patch default to zero"
        with pytest.raises(ValueError):
            main.Version(-1)
        with pytest.raises(TypeError):
            main.Version(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            main.Version(1, prerelease="01")

    def test_numeric_not_lexical_ordering(self, main: ModuleType) -> None:
        parse = main.Version.parse
        assert parse("1.9.0") < parse("1.10.0"), "core parts compare numerically"
        assert sorted([parse("2.0.0"), parse("1.10.0"), parse("1.9.0")]) == [
            parse("1.9.0"),
            parse("1.10.0"),
            parse("2.0.0"),
        ], "sorting uses semantic precedence"

    def test_prerelease_precedence_spec_example(self, main: ModuleType) -> None:
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        versions = [main.Version.parse(text) for text in ordered]
        assert sorted(reversed(versions)) == versions, "semver 2.0 spec ordering must hold"
        assert max(versions) == main.Version.parse("1.0.0"), "a release outranks its prereleases"

    def test_build_metadata_ignored_for_equality_and_hash(self, main: ModuleType) -> None:
        a = main.Version.parse("1.0.0+a")
        b = main.Version.parse("1.0.0+b")
        assert a == b, "build metadata does not affect precedence or equality"
        assert hash(a) == hash(b), "equal versions must hash equally"
        assert str(a) == "1.0.0+a", "build metadata is still displayed"
        assert main.Version.parse("1.0.0-rc.1") != main.Version.parse("1.0.0"), "prerelease does affect equality"

    def test_immutable(self, main: ModuleType) -> None:
        v = main.Version.parse("1.2.3")
        with pytest.raises(AttributeError):
            v.major = 2  # type: ignore[misc]
        with pytest.raises(AttributeError):
            v.anything = 1  # type: ignore[attr-defined]

    def test_comparison_with_non_version(self, main: ModuleType) -> None:
        v = main.Version.parse("1.0.0")
        assert (v == "1.0.0") is False, "equality with a string is False, not an error"
        with pytest.raises(TypeError):
            _ = v < "1.0.0"  # type: ignore[operator]

    def test_bump(self, main: ModuleType) -> None:
        v = main.Version.parse("1.2.3-rc.1+build.5")
        assert str(v.bump("minor")) == "1.3.0", "bumping minor resets patch and clears prerelease and build"
        assert str(v.bump("major")) == "2.0.0", "bumping major resets lower parts"
        assert str(v.bump("patch")) == "1.2.4", "bumping patch"
        assert str(v) == "1.2.3-rc.1+build.5", "bump returns a new Version and leaves the original alone"
        with pytest.raises(ValueError):
            v.bump("epoch")

    def test_hashable_as_dict_key(self, main: ModuleType) -> None:
        index = {main.Version.parse("1.0.0"): "stable"}
        assert index[main.Version.parse("1.0.0+ci.7")] == "stable", "lookups use precedence-based equality"
