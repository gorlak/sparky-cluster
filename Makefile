# Delegates all targets to ansible/Makefile — no target list to maintain.
%:
	@$(MAKE) -C ansible $* $(MAKEOVERRIDES)
