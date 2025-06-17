## Summary

<!-- What does this PR do? One sentence. -->

## Type of Change

- [ ] Bug fix
- [ ] New feature
- [ ] Performance improvement
- [ ] Refactor (no behavior change)
- [ ] Documentation
- [ ] Tests

## Testing

```bash
# Commands to verify this PR locally
pytest tests/unit/ -v
pytest tests/integration/ -v -m integration
```

- [ ] Unit tests pass (`pytest tests/unit/`)
- [ ] Integration tests pass (requires `docker compose up`)
- [ ] No existing tests broken
- [ ] New tests added for new behavior

## Checklist

- [ ] Code follows the existing style (black + isort + flake8)
- [ ] All `[CURSOR TO IMPLEMENT]` stubs in touched files are implemented
- [ ] No secrets or credentials committed
- [ ] `DEVELOPMENT.md` updated if setup steps changed
