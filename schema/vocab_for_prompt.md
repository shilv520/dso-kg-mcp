# DSO Textbook Reference-Knowledge Extraction Vocabulary (closed schema)

## Entity types (instantiate concrete types only)
- ActivityPattern: Object-backed reusable professional activity pattern inside a WorkflowPattern. It expresses what should be done at an atomic professional-process level, not a data-science problem/task category. It is distinct from Probl
    required: (none)
    optional: activityOrder, guidance, pitfalls
- Constraint: Normative or machine-checkable ReferenceClaim representing a hard requirement. Empirical evidence may motivate review but cannot silently override it.
    required: constraintSeverity
    constraintSeverity enum: error|warning|info
    optional: constraintLanguage, constraintExpression
- DataCharacteristic: Reusable analytical dimension for describing data, such as missingness, distribution shape, temporal structure, cardinality, sparsity, leakage risk, or feature relationship. A DataCondition states a concrete reusable con
    required: characteristicKind
    characteristicKind enum: missingness|distribution|outlier_behavior|class_balance|cardinality|sparsity|scale|temporal_structure|group_structure|feature_relationship|multicollinearity|distribution_shift|noise|sample_size|leakage_risk|semantic_role|other
- DataCondition: Reusable normalized state/condition of data (e.g., high missingness, strong skew, temporal drift) used as an applicability boundary. A quantitative condition MAY reference a DataStatistic and encode an operator/threshold
    required: dataConditionKind, conditionStatement
    dataConditionKind enum: missingness|outlier|skewness|imbalance|distribution_shift|temporal_dependency|group_structure|sparsity|high_cardinality|multicollinearity|noise|small_sample|other
    conditionOperator enum: eq|ne|gt|ge|lt|le|between|exists|custom
    optional: conditionNumericThreshold, conditionNumericUpperThreshold
- DataStatistic: Reusable quantitative data descriptor such as MissingRate, NullCount, Skewness, Cardinality, ClassImbalanceRatio or DriftScore. It defines what is computed; concrete observed values are DataStatisticMeasurement instances
    required: (none)
    optional: statisticFormula, measurementUnit
- DomainConcept: Reusable application-domain concept such as wellhead pressure, gas production rate, customer segment, or disease state.
    required: (none)
- EvaluationProtocol: Reusable evaluation/validation design such as random holdout, group k-fold, time-series split, rolling validation, or leaderboard evaluation. It is separate from Metric: Metric says what is measured; EvaluationProtocol s
    required: evaluationProtocolType
    evaluationProtocolType enum: holdout|kfold|stratified_kfold|group_kfold|time_series_split|rolling_window|backtesting|cross_validation|leaderboard|custom
    optional: splitStrategy, foldCount, leakageControlSummary
- Guideline: Advisory and overridable ReferenceClaim expressing recommendation, selection rule, procedure advice, or best practice under declared applicability conditions.
    required: guidelineType
    guidelineType enum: recommendation|selection_rule|procedure_advice|warning|best_practice|other
- IndustryDomain: Reusable application-domain area such as gas production, retail, finance, or medicine. This is application semantics, not a data-science task category.
    required: (none)
- KnowledgeSource: Bibliographic/provenance resource identifying a source document, repository, standard, notebook, code artifact, or expert source. Source authority does not by itself constitute empirical validation.
    required: sourceType, sourceTitle
    sourceType enum: textbook|handbook|standard|official_documentation|research_paper|review_paper|model_card|model_repository|competition_case|enterprise_sop|project_record|notebook|source_code|execution_log|expert_input|dataset_documentation|other
    optional: sourceUri, citation, locator, publicationDate, resourceVersion, license, authors, publisherOrVenue, doi, isbn
- Method: Reusable conceptual data-science method, algorithm, model family, transformation, analysis, or evaluation technique independent of one concrete implementation or trained artifact.
    required: methodPurpose, methodParadigm
    methodPurpose enum: data_profiling|data_cleaning|data_transformation|feature_engineering|modeling|optimization|evaluation|visualization|deployment|other
    methodParadigm enum: rule_based|statistical|machine_learning|deep_learning|hybrid|other
    trainingMode enum: not_applicable|training_free|train_from_scratch|pretrained|fine_tunable|other
    optional: assumptions, limitations, resourceRequirements
- MethodImplementation: Reusable implementation knowledge for a Method, such as a library class, function or model-repository artifact. Distinct from OMM ActionType, which is an executable capability contract.
    required: implementationType
    implementationType enum: library_api|class|function|cli|service_api|repository|model_repository|notebook|other
    optional: implementationVersion, programmingLanguage, callablePath, invocationTemplate, parameterSchema, inputSchema, outputSchema
- Metric: Reusable metric definition describing what is quantified; evaluation protocol is modeled separately.
    required: metricDirection
    metricDirection enum: minimize|maximize|target_range
    optional: metricFormula, measurementUnit
- ModelSpecification: Reusable normalized model-level specification that is more concrete than a conceptual Method but is not a case-trained ModelArtifact. It may be derived from architecture specifications, model-card fragments, repository m
    required: modelSpecificationType
    modelSpecificationType enum: architecture|estimator_configuration|pretrained_model|checkpoint_template|model_repository_asset|other
    optional: architectureSummary, parameterCount, repositoryUri, modelCardUri, pretrainingSummary, fineTuningSupport
- ProblemType: Reusable data-science task archetype (e.g., regression, classification, forecasting) separated from application-domain semantics.
    required: problemCategory
    problemCategory enum: regression|classification|ranking|clustering|forecasting|anomaly_detection|dimensionality_reduction|optimization|other
    optional: objectiveStatement, successCriteria
- ReferenceClaim: Reusable source-grounded proposition extracted from textbooks, standards, documentation, application-domain sources, or research literature. It is a normalized semantic assertion rather than a document summary. Its epist
    required: knowledgeOriginRole, referenceClaimStatus
    knowledgeOriginRole enum: data_science|application_domain|cross_domain
    referenceClaimStatus enum: source_asserted|literature_corroborated|consensus_or_standardized|hypothesis|contested|deprecated
    optional: revisionRationale
- SoftwareComponent: Reusable software package, module, service or repository component used by implementations.
    required: componentType
    componentType enum: library|distribution_package|import_package|module|class|function|cli|service|repository|model_family|other
    optional: resourceVersion, license
- SolutionTemplate: Reusable high-level solution skeleton for a ProblemType; it organizes reusable stages but is not a concrete case pipeline.
    required: (none)
    optional: templateVersion, preconditionSummary, postconditionSummary
- SourceFragment: Traceable sub-resource inside a KnowledgeSource. SourceFragment separates structural location (chapter/section/paragraph/table/code block) from semantic role (theory, method, experiment result, standard rule, etc.) and i
    required: fragmentType, fragmentRole, fragmentLocator
    fragmentType enum: chapter|section|subsection|paragraph|figure|table|appendix|code_block|metadata_record|other
    fragmentRole enum: definition|background_theory|domain_semantics|method_description|procedure_description|assumption_or_limitation|experiment_setup|experiment_result|discussion_or_conclusion|implementation_reference|model_card_section|standard_or_sop_rule|other
    optional: fragmentSummary, contentHash
- Stage: Reusable coarse-grained phase of a SolutionTemplate.
    required: (none)
    optional: stageOrder
- TransitionPattern: Object-backed reusable transition knowledge inside a WorkflowPattern. It preserves corpus provenance and can be mapped to case transitions.
    required: (none)
    optional: transitionCondition, guidance, pitfalls
- WorkflowPattern: Object-backed reusable workflow knowledge extracted or curated from reference corpora. It supports provenance, case mapping and evidence relations. It is deliberately distinct from OMM ProcedureType, which is an M1 execu
    required: (none)
    optional: templateVersion, preconditionSummary, postconditionSummary

## Relation types (only these predicates are allowed)
### source: ClaimResource
- claimAppliesToProblemType -> ProblemType: Scopes a claim to one or more reusable problem types.
- claimAppliesToStage -> Stage: Associates a claim with a reusable Stage.
- claimAppliesUnderDataCondition -> DataCondition: States reusable data conditions under which a claim applies.
- claimConcernsActivityPattern -> ActivityPattern: Associates a claim with a reusable ActivityPattern; claims remain first-class proposition objects rather than being embedded only as activit
- claimConcernsDataCharacteristic -> DataCharacteristic: Links a reusable claim to the data characteristic dimension it discusses.
- claimConcernsDataCondition -> DataCondition: Links a claim to a DataCondition it explicitly describes or reasons about; applicabilityUnder remains a separate relation.
- claimConcernsDomainConcept -> DomainConcept: Scopes a claim or insight to relevant application-domain concepts.
- claimConcernsEvaluationProtocol -> EvaluationProtocol: Links a reusable claim to validation/evaluation protocol knowledge.
- claimConcernsMethod -> Method: Associates a claim with a Method.
- claimConcernsMethodImplementation -> MethodImplementation: Links implementation-specific claims to callable/API/repository implementations.
- claimConcernsMetric -> Metric: Links a reusable claim to an evaluation metric it defines, compares, limits, or recommends.
- claimConcernsModelSpecification -> ModelSpecification: Links claims from model cards, repositories, papers, or documentation to reusable model specifications.
- claimConcernsProblemType -> ProblemType: Links a claim to a problem type it defines, characterizes, compares, or otherwise discusses; this is distinct from applicability.
- claimConcernsSoftwareComponent -> SoftwareComponent: Links documentation, compatibility, version, or implementation claims to software components.
- claimConcernsSolutionTemplate -> SolutionTemplate: Associates a claim with a reusable SolutionTemplate.
- claimConcernsStage -> Stage: Links a claim to a Stage it defines or discusses, distinct from the claim being applicable at that stage.
- claimConcernsWorkflowPattern -> WorkflowPattern: Associates a claim with a reusable object-backed WorkflowPattern.
- claimExcludedUnderDataCondition -> DataCondition: Declares a reusable data condition under which a ClaimResource must not be treated as applicable.
- claimGroundedInFragment -> SourceFragment: Provides claim-level fine-grained provenance. A claim should prefer this relation over whole-document-only provenance when a stable fragment
- claimInIndustryDomain -> IndustryDomain: Scopes a reusable claim or insight to an IndustryDomain.
### source: DataCharacteristic
- dataCharacteristicBroaderCharacteristic -> DataCharacteristic: Optional hierarchy among reusable data-characteristic dimensions.
### source: DataCondition
- dataConditionBasedOnStatistic -> DataStatistic: Optionally grounds a quantitative DataCondition in the reusable DataStatistic whose measurement is compared with its threshold.
- dataConditionCharacterizes -> DataCharacteristic: Associates a normalized DataCondition with the reusable DataCharacteristic dimension it constrains.
- dataConditionRefinesDataCondition -> DataCondition: Expresses a more specific reusable data condition relative to a broader condition without turning the condition into a case observation.
- dataConditionUsesDomainConcept -> DomainConcept: Specializes a reusable data condition with application-domain semantics when needed.
### source: DataStatistic
- dataStatisticMeasuresCharacteristic -> DataCharacteristic: Links a reusable quantitative DataStatistic to the DataCharacteristic dimension it operationalizes.
### source: DomainConcept
- domainConceptBroaderConcept -> DomainConcept: Optional broader/narrower hierarchy among reusable application-domain concepts.
- domainConceptInIndustryDomain -> IndustryDomain: Places a DomainConcept in one or more IndustryDomains.
### source: Entity
- resourceHasSource -> KnowledgeSource: Links any DSO instance resource to source provenance. This relation does not assert empirical validation.
### source: IndustryDomain
- industryDomainBroaderDomain -> IndustryDomain: Optional reusable hierarchy between application domains.
### source: KnowledgeSource
- sourceHasFragment -> SourceFragment: Decomposes a source into traceable fragments. Every SourceFragment belongs to exactly one KnowledgeSource; a source may have zero or many fr
### source: Method
- methodApplicableToProblemType -> ProblemType: Reusable applicability of a Method to a ProblemType.
- methodApplicableUnderDataCondition -> DataCondition: Reusable applicability of a Method under a DataCondition.
- methodBroaderMethod -> Method: Represents method taxonomy without assuming transitive reasoning in RelationType semantics.
- methodExcludedUnderDataCondition -> DataCondition: Declares a reusable data condition that excludes or invalidates normal applicability of a Method.
- methodRealizesActivityPattern -> ActivityPattern: Links a conceptual Method to the reusable ActivityPattern(s) it can realize. ActivityPattern answers what should be done; Method answers the
### source: MethodImplementation
- implementationConformsToModelSpecification -> ModelSpecification: Links an implementation entry to a reusable model specification when the implementation realizes a particular architecture/configuration/ass
- implementationImplementsMethod -> Method: Links a reusable technical implementation to the conceptual Method it implements.
- implementationUsesSoftwareComponent -> SoftwareComponent: Links a method implementation to required software components.
### source: ModelSpecification
- modelSpecificationSpecifiesMethod -> Method: Associates a reusable normalized model specification, possibly derived from model-card or repository metadata, with the conceptual method or
- modelSpecificationUsesSoftwareComponent -> SoftwareComponent: Associates a reusable model specification with libraries, repositories, modules, or services required to obtain or use it.
### source: ProblemType
- problemTypeBroaderProblemType -> ProblemType: Allows domain-scoped task archetypes such as gas-well production forecasting to specialize broader data-science problem types such as foreca
- problemTypeEvaluatedByMetric -> Metric: Links a reusable problem archetype to suitable metrics.
- problemTypeInIndustryDomain -> IndustryDomain: Associates a reusable ProblemType with an application domain.
- problemTypeUsesDomainConcept -> DomainConcept: Identifies domain concepts relevant to a reusable problem archetype.
- problemTypeUsesEvaluationProtocol -> EvaluationProtocol: Associates reusable task types with commonly applicable evaluation protocols without making the protocol mandatory for every case.
### source: ReferenceClaim
- referenceClaimRevisionOf -> ReferenceClaim: Links a newer curated ReferenceClaim to at most one immediate prior ReferenceClaim version, preserving reference-evolution lineage.
### source: ReferenceResource
- resourceGroundedInFragment -> SourceFragment: Links a reusable semantic resource or claim to the exact source fragment from which it was extracted or normalized.
### source: SoftwareComponent
- softwareComponentPartOf -> SoftwareComponent: Represents software containment or packaging.
### source: SolutionTemplate
- solutionTemplateApplicableToProblemType -> ProblemType: Scopes a solution template to reusable problem archetypes.
- solutionTemplateIncludesStage -> Stage: Composes a high-level solution template from reusable stages.
### source: SourceFragment
- fragmentPartOfFragment -> SourceFragment: Represents nested source structure such as paragraph within section or section within chapter. A fragment may have at most one immediate par
### source: Stage
- stageContainsWorkflowPattern -> WorkflowPattern: Places reusable workflow patterns in a stage.
### source: TransitionPattern
- transitionPatternSourceActivity -> ActivityPattern: Source task of an object-backed transition pattern.
- transitionPatternTargetActivity -> ActivityPattern: Target task of an object-backed transition pattern.
### source: WorkflowPattern
- workflowPatternApplicableToProblemType -> ProblemType: Scopes workflow patterns to reusable problem types.
- workflowPatternApplicableUnderDataCondition -> DataCondition: Scopes workflow patterns to reusable data conditions.
- workflowPatternContainsActivityPattern -> ActivityPattern: Composes an object-backed WorkflowPattern from reusable ActivityPatterns.
- workflowPatternContainsTransitionPattern -> TransitionPattern: Composes an object-backed workflow pattern from reusable transition patterns.
- workflowPatternEntryActivity -> ActivityPattern: Marks reusable entry ActivityPatterns.
- workflowPatternExcludedUnderDataCondition -> DataCondition: Declares a data condition under which a WorkflowPattern should not be considered applicable.
- workflowPatternExitActivity -> ActivityPattern: Marks reusable exit ActivityPatterns.

## Abstract types (do not instantiate; use concrete subtypes)
- ClaimResource (subtypes: ReferenceClaim)
- Entity (subtypes: ClaimResource, KnowledgeSource, ReferenceResource, SourceFragment)
- ReferenceResource (subtypes: ActivityPattern, DataCharacteristic, DataCondition, DataStatistic, DomainConcept, EvaluationProtocol, IndustryDomain, Method, MethodImplementation, Metric, ModelSpecification, ProblemType, SoftwareComponent, SolutionTemplate, Stage, TransitionPattern, WorkflowPattern)