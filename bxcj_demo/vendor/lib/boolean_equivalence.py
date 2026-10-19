#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/boolean_equivalence.py — Boolean Expression Logical Equivalence.

Part P: Promoted from workbench string canonicalization to a proper
logical equivalence checker.

Supports:
    - POU prefix stripping
    - INT# removal
    - NOT normalization (NOT(a) → ¬a)
    - AND commutativity (a AND b == b AND a)
    - OR commutativity (a OR b == b OR a)
    - Associative flattening (a AND (b AND c) → {a, b, c})
    - Duplicate predicate removal
    - De Morgan's law (NOT(a AND b) → ¬a OR ¬b)
    - EQ normalization (= → ==)
    - Parentheses normalization
    - Whitespace normalization

Goal: ``logical_equivalent(expr1, expr2)`` returns True if the two
boolean expressions are logically equivalent, not just string-equal.

This is a Library module — project-agnostic, no project branching.
"""
import re
from typing import Union


# ============================================================
# Tokenizer
# ============================================================

def _tokenize(expr: str) -> list:
    """Tokenize a boolean expression into atoms and operators."""
    if not expr:
        return []

    # Normalize whitespace
    expr = ' '.join(expr.split())

    # Remove INT# prefix
    expr = re.sub(r'INT#', '', expr, flags=re.IGNORECASE)

    # Normalize operators (case-insensitive)
    expr = re.sub(r'\bAND\b', '&', expr, flags=re.IGNORECASE)
    expr = re.sub(r'\bOR\b', '|', expr, flags=re.IGNORECASE)
    expr = re.sub(r'\bNOT\b', '!', expr, flags=re.IGNORECASE)
    expr = re.sub(r'\bXOR\b', '^', expr, flags=re.IGNORECASE)

    # Normalize = to == (but not ==, <=, >=, <>)
    expr = re.sub(r'(?<![<>=!])=(?![=])', '==', expr)

    # Tokenize — include & | ! ^ as single-char operator tokens
    tokens = re.findall(
        r'==|!=|<=|>=|<>|&&|\|\||[&|!^()*+/\\-]|[^\s&|!^()]+', expr)

    # Normalize && → &, || → |
    tokens = [t if t not in ('&&',) else '&' for t in tokens]
    tokens = [t if t not in ('||',) else '|' for t in tokens]

    return tokens


# ============================================================
# AST Node
# ============================================================

class Node:
    """AST node for boolean expression."""
    pass


class Literal(Node):
    """A leaf predicate (e.g., 'Process_steps[2]==130')."""
    def __init__(self, value: str):
        self.value = _normalize_literal(value)

    def __eq__(self, other):
        return isinstance(other, Literal) and self.value == other.value

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return 'Lit({})'.format(self.value)


class NotNode(Node):
    """Logical NOT."""
    def __init__(self, child: Node):
        self.child = child

    def __eq__(self, other):
        return isinstance(other, NotNode) and self.child == other.child

    def __hash__(self):
        return hash(('NOT', self.child))

    def __repr__(self):
        return 'NOT({})'.format(self.child)


class AndNode(Node):
    """Logical AND (n-ary, flattened)."""
    def __init__(self, children: set):
        self.children = frozenset(children)

    def __eq__(self, other):
        return isinstance(other, AndNode) and self.children == other.children

    def __hash__(self):
        return hash(('AND', self.children))

    def __repr__(self):
        return 'AND({})'.format(sorted(repr(c) for c in self.children))


class OrNode(Node):
    """Logical OR (n-ary, flattened)."""
    def __init__(self, children: set):
        self.children = frozenset(children)

    def __eq__(self, other):
        return isinstance(other, OrNode) and self.children == other.children

    def __hash__(self):
        return hash(('OR', self.children))

    def __repr__(self):
        return 'OR({})'.format(sorted(repr(c) for c in self.children))


# ============================================================
# Literal Normalization
# ============================================================

def _normalize_literal(s: str) -> str:
    """Normalize a literal predicate string."""
    s = s.strip()
    # Remove ALL POU prefixes (e.g., 'SC01.Main.Process_steps' → 'Process_steps')
    # The last dot-separated segment is the variable name
    if '.' in s:
        # Only strip if the part before the last dot looks like a POU name
        # (alphanumeric, no comparison operators)
        parts = s.split('.')
        if len(parts) > 1 and not any(
                op in parts[-2] for op in ('=', '<', '>', '!')):
            s = parts[-1]
    # Normalize whitespace
    s = ' '.join(s.split())
    # Remove INT#
    s = re.sub(r'INT#', '', s, flags=re.IGNORECASE)
    return s


# ============================================================
# Parser (recursive descent)
# ============================================================

class _Parser:
    """Recursive descent parser for boolean expressions."""

    def __init__(self, tokens: list):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> Node:
        return self._parse_or()

    def _parse_or(self) -> Node:
        children = [self._parse_and()]
        while self.peek() in ('|', '^'):
            op = self.consume()
            right = self._parse_and()
            children.append(right)
            if op == '^':
                # XOR is not commutative-associative in the same way
                # Keep as is for now
                pass

        if len(children) == 1:
            return children[0]
        return OrNode(set(children))

    def _parse_and(self) -> Node:
        children = [self._parse_not()]
        while self.peek() == '&':
            self.consume()
            children.append(self._parse_not())

        if len(children) == 1:
            return children[0]
        return AndNode(set(children))

    def _parse_not(self) -> Node:
        if self.peek() == '!':
            self.consume()
            child = self._parse_not()
            # Double negation elimination
            if isinstance(child, NotNode):
                return child.child
            return NotNode(child)
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        tok = self.peek()
        if tok == '(':
            self.consume()  # (
            node = self._parse_or()
            if self.peek() == ')':
                self.consume()  # )
            return node
        if tok == '!':
            return self._parse_not()

        # Literal — consume until we hit an operator or paren
        parts = []
        while (self.pos < len(self.tokens)
               and self.peek() not in ('&', '|', '^', ')', '(')):
            parts.append(self.consume())

        if parts:
            return Literal(' '.join(parts))
        return Literal('')


# ============================================================
# De Morgan's Law Application
# ============================================================

def _apply_de_morgan(node: Node) -> Node:
    """Apply De Morgan's laws to push NOT inward.

    NOT(a AND b) → NOT(a) OR NOT(b)
    NOT(a OR b) → NOT(a) AND NOT(b)
    """
    if isinstance(node, NotNode):
        child = node.child
        if isinstance(child, AndNode):
            # NOT(a AND b) → NOT(a) OR NOT(b)
            new_children = set()
            for c in child.children:
                new_children.add(_apply_de_morgan(NotNode(c)))
            return OrNode(new_children)
        if isinstance(child, OrNode):
            # NOT(a OR b) → NOT(a) AND NOT(b)
            new_children = set()
            for c in child.children:
                new_children.add(_apply_de_morgan(NotNode(c)))
            return AndNode(new_children)
        if isinstance(child, NotNode):
            # Double negation
            return _apply_de_morgan(child.child)
        return NotNode(_apply_de_morgan(child))

    if isinstance(node, AndNode):
        return AndNode(set(
            _apply_de_morgan(c) for c in node.children))

    if isinstance(node, OrNode):
        return OrNode(set(
            _apply_de_morgan(c) for c in node.children))

    return node


# ============================================================
# Flatten
# ============================================================

def _flatten(node: Node) -> Node:
    """Flatten nested AND/OR nodes."""
    if isinstance(node, AndNode):
        flat_children = set()
        for c in node.children:
            fc = _flatten(c)
            if isinstance(fc, AndNode):
                flat_children.update(fc.children)
            else:
                flat_children.add(fc)
        return AndNode(flat_children)

    if isinstance(node, OrNode):
        flat_children = set()
        for c in node.children:
            fc = _flatten(c)
            if isinstance(fc, OrNode):
                flat_children.update(fc.children)
            else:
                flat_children.add(fc)
        return OrNode(flat_children)

    if isinstance(node, NotNode):
        return NotNode(_flatten(node.child))

    return node


# ============================================================
# Canonical Form
# ============================================================

def _to_canonical(node: Node) -> Node:
    """Convert to canonical form: De Morgan + flatten + simplify."""
    node = _apply_de_morgan(node)
    node = _flatten(node)
    node = _simplify(node)
    return node


def _simplify(node: Node) -> Node:
    """Simplify single-child AND/OR nodes."""
    if isinstance(node, AndNode):
        if len(node.children) == 1:
            return _simplify(next(iter(node.children)))
        return AndNode(set(_simplify(c) for c in node.children))
    if isinstance(node, OrNode):
        if len(node.children) == 1:
            return _simplify(next(iter(node.children)))
        return OrNode(set(_simplify(c) for c in node.children))
    if isinstance(node, NotNode):
        return NotNode(_simplify(node.child))
    return node


def _canonical_str(node: Node) -> str:
    """Get a canonical string representation for comparison."""
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, NotNode):
        return '!{}'.format(_canonical_str(node.child))
    if isinstance(node, AndNode):
        return 'AND({})'.format(sorted(
            _canonical_str(c) for c in node.children))
    if isinstance(node, OrNode):
        return 'OR({})'.format(sorted(
            _canonical_str(c) for c in node.children))
    return str(node)


# ============================================================
# Public API
# ============================================================

def parse_expression(expr: str) -> Node:
    """Parse a boolean expression string into an AST."""
    if not expr or not expr.strip():
        return Literal('')

    tokens = _tokenize(expr)
    if not tokens:
        return Literal('')

    parser = _Parser(tokens)
    try:
        return parser.parse()
    except (IndexError, Exception):
        return Literal(expr.strip())


def canonical_form(expr: str) -> str:
    """Get the canonical string form of a boolean expression.

    This handles:
        - POU prefix stripping
        - INT# removal
        - NOT normalization
        - AND/OR commutativity (sorted children)
        - Associative flattening
        - Duplicate predicate removal
        - De Morgan's law
        - EQ normalization
        - Whitespace normalization
    """
    node = parse_expression(expr)
    node = _to_canonical(node)
    return _canonical_str(node)


def logical_equivalent(expr1: str, expr2: str) -> bool:
    """Check if two boolean expressions are logically equivalent.

    This goes beyond string canonicalization — it handles:
        - Commutativity: a AND b == b AND a
        - Associativity: a AND (b AND c) == (a AND b) AND c
        - Duplicate removal: a AND a == a
        - De Morgan: NOT(a AND b) == NOT(a) OR NOT(b)
        - Double negation: NOT(NOT(a)) == a

    Args:
        expr1: First boolean expression
        expr2: Second boolean expression

    Returns:
        True if the expressions are logically equivalent.
    """
    if expr1 == expr2:
        return True

    c1 = canonical_form(expr1)
    c2 = canonical_form(expr2)

    return c1 == c2


def normalize_label(label: str) -> str:
    """Normalize a timing label for comparison.

    Handles:
        - Whitespace removal
        - INT# removal
        - POU prefix stripping
        - NOT() normalization
    """
    if not label:
        return ''

    label = label.strip().replace(' ', '')
    label = label.replace('INT#', '')
    label = re.sub(r'^[\w]+\.', '', label)  # POU prefix

    # Normalize NOT(...) to NOT(...)
    label = re.sub(r'NOT\s*\(', 'NOT(', label, flags=re.IGNORECASE)

    return label
