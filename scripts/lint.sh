#!/bin/bash

ruff check . --fix
ruff format .
djlint app/templates --reformat
npx prettier . --write