from marshmallow import Schema, fields


class SplitSetObjectSchema(Schema):
    ratio = fields.Int(description="Ratio of dataset", example=60)
    file = fields.String(description="File indices", example="./data/indices.csv")


class SplitObjectSchema(Schema):
    train = fields.Nested(SplitSetObjectSchema, required=True)
    val = fields.Nested(SplitSetObjectSchema, required=False, missing=None)
    test = fields.Nested(SplitSetObjectSchema, required=True)


class BaseDatasetSchema(Schema):
    batch_size = fields.Int(missing=4, description="Batch size", example=4)
    shuffle = fields.Bool(
        missing=True, description="Should shuffle dataset", example=False
    )
    num_workers = fields.Int(missing=2, description="Number of workers", example=2)


class SplitableDatasetSchema(BaseDatasetSchema):
    split = fields.Nested(
        SplitObjectSchema,
        description="Configuration on how to split the dataset.",
        missing=None,
    )
    override = fields.Bool(
        missing=False,
        default="Should override split files if they exist.",
        example=False,
    )
