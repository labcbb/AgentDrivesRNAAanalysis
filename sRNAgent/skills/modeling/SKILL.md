---
name: modeling
description: "Prepare AnnData expression and clinical variables, select features, and fit classification or Cox survival models. Use for clinical covariate encoding, missing-value and low-variance filtering, differential-expression-driven selection, classification models, or univariate/multivariate Cox with Lasso/Ridge/ElasticNet and cross-validation."
---

# Feature Selection and Modeling

This skill covers two modelling routes on one supplied AnnData: classification and Cox survival analysis. Use `sa.model.feature_selection` before either route when expression or clinical candidates need preprocessing. Store all model records in `adata.uns` and save the completed AnnData H5AD.

## Common Preparation

Keep expression variables in `adata.var_names` and pass clinical variables from `adata.obs` through `obs_features`. Explicitly identify the target column or the Cox time/event columns; never infer them from sample names.

```python
adata = sa.model.feature_selection(
    adata,
    features=candidate_features,
    obs_features=["Stage", "Age", "Sex"],
    layer="logcpm",
    missing_threshold=0.20,
    imputation="median",
    variance_threshold=0.0,
)
```

Use these defaults unless the data justify a change:

- Drop columns with more than 20% missing values.
- Impute numeric variables with the median. Use mean, most-frequent, or iterative imputation only with a stated rationale.
- Encode categorical clinical variables with one-hot encoding. The first sorted level is the reference; inspect it in `adata.uns["feature_selection"]`.
- Remove zero-variance features. Apply a positive variance threshold only when the expression scale makes it interpretable.
- For high-dimensional expression data, reduce candidates before fitting an unpenalised model.

| Task | Default feature selection | Alternative |
|---|---|---|
| DE-derived biomarkers | `method="de"`, `de_pvalue=0.05` | Set `top_k` to cap candidates |
| Classification | `feature_selection="mutual_info"`, `selection_top_k=20` | `f_classif` for ANOVA screening |
| Outcome-independent filtering | `method="variance"` | Set `variance_threshold` |
| Cox multivariable model | `selection="lasso"` | Ridge for correlated variables, ElasticNet for both selection and grouping |

`method="de"` uses `adata.uns["de_results"]`, preferring adjusted p-values. It is suitable for candidate generation. Do supervised feature selection inside training folds whenever the objective is unbiased holdout or cross-validation performance.

## Classification Route

Use this route for a binary or multiclass outcome in `adata.obs`. Start with logistic regression for a compact clinical/expression model; use `model=None` to compare SVM, random forest, XGBoost, and logistic regression where sample size permits. Enable cross-validation for reported performance.

```python
adata = sa.model.classification(
    adata,
    features=classifier_candidates,
    group_col="response",
    obs_features=["Stage", "Age"],
    feature_selection="mutual_info",
    selection_top_k=20,
    split_data=True,
    cross_validate=True,
    cv_folds=5,
    model="logistic_regression",
    output_dir="classification_models",
)
```

Inspect `adata.uns["classification"]["performance"]` for holdout, cross-validation, and all-sample metrics. Treat only holdout or cross-validation metrics as performance estimates; all-sample values are descriptive. The `preprocessing` record contains reference levels, imputations, dropped variables, and selected feature names.

## Cox Survival Route

Use this route only when a positive survival-time column and a binary event column are available. Cox checks the endpoint, counts events, and reports EPV (events per fitted variable); the default warns below EPV 10. Use `strict_epv=True` to block a low-EPV fit.

```python
adata = sa.model.cox(
    adata,
    features=cox_candidates,
    time_col="os_time",
    event_col="os_event",
    obs_features=["Stage", "Age"],
    analysis="both",
    selection="lasso",
    max_features=10,
    cross_validate=True,
    cv_folds=5,
    output_dir="cox_models",
)
```

Use `analysis="univariate"` to screen single variables, `"multivariate"` for a joint model, and `"both"` when both outputs are requested. Use Lasso by default after high-dimensional screening; select Ridge when retaining correlated predictors is desired, and ElasticNet when both sparsity and correlated-feature stability are required.

Review `adata.uns["cox"]`:

- `univariate_results` and `multivariate_results`: coefficient, hazard ratio, confidence interval, and p-value tables.
- `selection_coefficients` and `selected_features`: penalised selection results.
- `endpoint_validation`: time/status validation, event count, and EPV.
- `cross_validation`: held-out C-index by fold.

## Save Results

```python
adata.write("modeling_results.h5ad")
```

Model artifact paths are recorded in `adata.uns["cox"]["model_path"]` and `adata.uns["classification"]["model_paths"]`. For read-only result questions, load these stored results instead of rerunning the model.
